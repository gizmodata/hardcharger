//! The SQL workspace without the local server: `hardcharger ui` (the bundled client) serves
//! the same web UI on a loopback port of its own, with no engine behind it, and relays the
//! workspace's requests to the remote servers the person adds. It runs while the workspace
//! window is open and is stopped when the window closes (or the app quits).

use std::{
    io::{BufRead, BufReader},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::mpsc,
    time::Duration,
};

pub struct UiSidecar {
    child: Child,
    /// The UI's origin (http://127.0.0.1:<port>), from the line the command prints.
    pub origin: String,
}

impl UiSidecar {
    /// Starts `hardcharger ui --no-open --quiet --listen 127.0.0.1:0` and reads the URL it
    /// prints. Plain HTTP on loopback: the relay still speaks TLS to the remote servers.
    pub fn start(client: &PathBuf) -> Result<Self, String> {
        let mut command = Command::new(client);
        command
            .args(["ui", "--no-open", "--quiet", "--listen", "127.0.0.1:0"])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        // The desktop app's own HARDCHARGER_* variables are the server's: the UI takes none.
        for (key, _) in std::env::vars_os() {
            if key.to_string_lossy().starts_with("HARDCHARGER_") {
                command.env_remove(key);
            }
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        }
        let mut child = command.spawn().map_err(|e| {
            format!(
                "Could not start the SQL workspace ({}): {e}",
                client.display()
            )
        })?;
        let stdout = child
            .stdout
            .take()
            .ok_or("The workspace printed nothing.")?;
        let stderr = child.stderr.take();
        // The URL line, or the command's error, within a few seconds.
        let (sender, receiver) = mpsc::channel::<Result<String, String>>();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut line = String::new();
            let read = match reader.read_line(&mut line) {
                Ok(0) => Err("The workspace exited before it printed its address.".to_string()),
                Ok(_) => Ok(line.trim().to_string()),
                Err(e) => Err(e.to_string()),
            };
            let _ = sender.send(read);
        });
        let stderr_text = std::sync::Arc::new(std::sync::Mutex::new(String::new()));
        if let Some(stderr) = stderr {
            let collected = stderr_text.clone();
            std::thread::spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    if let Ok(mut text) = collected.lock() {
                        text.push_str(&line);
                        text.push('\n');
                    }
                }
            });
        }
        match receiver.recv_timeout(Duration::from_secs(15)) {
            Ok(Ok(line)) if line.starts_with("http://127.0.0.1:") && line.ends_with("/ui/") => {
                let origin = line.trim_end_matches("/ui/").to_string();
                Ok(Self { child, origin })
            }
            Ok(Ok(line)) => {
                let _ = child.kill();
                let _ = child.wait();
                Err(format!("The workspace printed an unexpected line: {line}"))
            }
            Ok(Err(why)) => {
                let _ = child.wait();
                let detail = stderr_text.lock().map(|t| t.clone()).unwrap_or_default();
                Err(format!("{why} {}", detail.trim()).trim().to_string())
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                Err("The workspace did not start within 15 seconds.".into())
            }
        }
    }

    #[cfg(test)]
    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    /// Whether the process is still running.
    pub fn running(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    /// Stops it (a clean stop request, then a kill after a few seconds) and waits.
    pub fn stop(&mut self) {
        if self.child.try_wait().ok().flatten().is_some() {
            return;
        }
        let _ = crate::server::request_stop(self.child.id());
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while self.child.try_wait().ok().flatten().is_none() {
            if std::time::Instant::now() >= deadline {
                let _ = self.child.kill();
                break;
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        let _ = self.child.wait();
    }
}

impl Drop for UiSidecar {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Needs HARDCHARGER_DESKTOP_TEST_CLIENT: the `hardcharger` client with the UI embedded.
    #[test]
    fn starts_serves_the_ui_and_exits_when_stopped() {
        let Some(client) = std::env::var_os("HARDCHARGER_DESKTOP_TEST_CLIENT") else {
            eprintln!("Set HARDCHARGER_DESKTOP_TEST_CLIENT to exercise the UI sidecar.");
            return;
        };
        let mut sidecar = UiSidecar::start(&PathBuf::from(client)).unwrap();
        assert!(sidecar.origin.starts_with("http://127.0.0.1:"));
        let pid = sidecar.pid();
        let contract: serde_json::Value =
            reqwest::blocking::get(format!("{}/ui/v1/app", sidecar.origin))
                .unwrap()
                .json()
                .unwrap();
        assert_eq!(contract["local_server"], false);
        assert_eq!(contract["remote_servers"], true);
        let page = reqwest::blocking::get(format!("{}/ui/", sidecar.origin)).unwrap();
        assert!(page.status().is_success());
        assert!(sidecar.running());
        sidecar.stop();
        assert!(!sidecar.running());
        // Nothing answers on its port any more, and the process is gone.
        assert!(reqwest::blocking::get(format!("{}/ui/v1/app", sidecar.origin)).is_err());
        #[cfg(unix)]
        assert!(
            !std::path::Path::new(&format!("/proc/{pid}")).exists() || cfg!(target_os = "macos")
        );
        let _ = pid;
    }

    #[test]
    fn a_missing_client_is_a_clear_error() {
        let error = UiSidecar::start(&PathBuf::from("/no/such/hardcharger"))
            .err()
            .unwrap();
        assert!(
            error.contains("Could not start the SQL workspace"),
            "{error}"
        );
    }
}
