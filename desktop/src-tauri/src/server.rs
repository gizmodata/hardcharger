use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom},
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

#[derive(Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Settings {
    pub port: u16,
    pub start_on_open: bool,
    pub server_options: BTreeMap<String, serde_json::Value>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            port: 31337,
            start_on_open: true,
            server_options: BTreeMap::new(),
        }
    }
}

impl Settings {
    pub fn validate(&self) -> Result<(), String> {
        if self.port < 1024 {
            return Err("Choose a port between 1024 and 65535.".into());
        }
        Ok(())
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    pub running: bool,
    pub pid: Option<u32>,
    pub port: u16,
    pub endpoint: String,
    pub database: String,
    pub log_file: String,
    pub uptime_seconds: u64,
    pub database_bytes: u64,
    pub version: String,
    pub error: Option<String>,
    pub settings: Settings,
}

pub struct Server {
    binary: PathBuf,
    pub directory: PathBuf,
    child: Option<Child>,
    started: Option<Instant>,
    password: String,
    pub settings: Settings,
    error: Option<String>,
    version: String,
    schema: Vec<serde_json::Value>,
}

const REDACTED: &str = "__HARDCHARGER_STORED_SECRET__";

// Fixed string domains validated by server/src/config.cc. Keep free-form strings
// (paths, catalog names, roles, DuckDB memory/storage settings) as text fields.
fn option_choices(name: &str) -> &'static [&'static str] {
    match name {
        "capture_query_profile" => &["off", "standard", "detailed"],
        "log_level" | "query_log_level" | "auth_log_level" | "session_log_level" => {
            &["debug", "info", "warning", "warn", "error"]
        }
        "log_format" => &["text", "json"],
        "log_emoji" => &["auto", "always", "never"],
        "mtls_identity_source" => &["san_email", "san_uri", "subject_cn"],
        _ => &[],
    }
}

fn choices_ignore_case(name: &str) -> bool {
    matches!(
        name,
        "log_level" | "query_log_level" | "auth_log_level" | "session_log_level" | "log_emoji"
    )
}

fn validate_option_choice(name: &str, value: &serde_json::Value) -> Result<(), String> {
    let choices = option_choices(name);
    if !choices.is_empty()
        && !value.as_str().is_some_and(|v| {
            choices.iter().any(|choice| {
                if choices_ignore_case(name) {
                    choice.eq_ignore_ascii_case(v)
                } else {
                    *choice == v
                }
            })
        })
    {
        return Err(format!("{name} must be one of: {}.", choices.join(", ")));
    }
    Ok(())
}

fn is_secret(name: &str) -> bool {
    matches!(
        name,
        "admin_password"
            | "secret_key"
            | "oauth_client_secret"
            | "audit_chain_key"
            | "rbac_store_uri"
            | "instrumentation_db_path"
            | "log_catalog_db_path"
            | "init_sql_commands"
    )
}

fn managed(name: &str) -> bool {
    matches!(
        name,
        "host"
            | "web_ui"
            | "health_port"
            | "metrics_port"
            | "metrics_bind_address"
            | "admin_username"
            | "admin_password"
            | "secret_key"
            | "tls_cert"
            | "tls_key"
            | "log_file"
            | "sensitive_paths"
            | "graceful_shutdown"
            | "shutdown_grace_period_seconds"
    ) || name.starts_with("mtls_")
}

fn credential_store() -> Result<keyring::Entry, String> {
    keyring::Entry::new(
        "com.gizmodata.hardcharger.desktop.preview",
        "server-options",
    )
    .map_err(|e| e.to_string())
}

fn public_settings(settings: &Settings) -> Settings {
    let mut result = settings.clone();
    for (key, value) in &mut result.server_options {
        if is_secret(key) && value.as_str().is_some_and(|v| !v.is_empty()) {
            *value = REDACTED.into();
        }
    }
    result
}

fn random_secret() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|e| e.to_string())?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

impl Server {
    pub fn new(binary: PathBuf, directory: PathBuf) -> Result<Self, String> {
        fs::create_dir_all(&directory).map_err(|e| e.to_string())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))
                .map_err(|e| e.to_string())?;
        }
        let mut settings = match fs::read(directory.join("settings.json")) {
            Ok(bytes) => serde_json::from_slice::<Settings>(&bytes)
                .map_err(|e| format!("Invalid desktop settings: {e}"))?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Settings::default(),
            Err(e) => return Err(e.to_string()),
        };
        if settings
            .server_options
            .values()
            .any(|v| v.as_str() == Some(REDACTED))
        {
            let stored = credential_store()?.get_password().map_err(|e| {
                format!("Could not read server credentials from the OS credential store: {e}")
            })?;
            let secrets: BTreeMap<String, serde_json::Value> =
                serde_json::from_str(&stored).map_err(|e| e.to_string())?;
            for (name, value) in &mut settings.server_options {
                if value.as_str() == Some(REDACTED) {
                    *value = secrets
                        .get(name)
                        .ok_or_else(|| format!("Missing stored credential for {name}."))?
                        .clone();
                }
            }
        }
        settings.validate()?;
        let output = Command::new(&binary)
            .arg("--version")
            .output()
            .map_err(|e| format!("Could not run bundled server: {e}"))?;
        if !output.status.success() {
            return Err("The bundled server could not report its version.".into());
        }
        let version = String::from_utf8_lossy(&output.stdout).trim().to_owned();
        let output = Command::new(&binary)
            .arg("--print-options-json")
            .output()
            .map_err(|e| e.to_string())?;
        if !output.status.success() {
            return Err("Could not read the bundled server's settings schema.".into());
        }
        let schema: serde_json::Value =
            serde_json::from_slice(&output.stdout).map_err(|e| e.to_string())?;
        let schema = schema["options"]
            .as_array()
            .ok_or("The server returned an invalid options schema.")?
            .clone();
        let result = Self {
            binary,
            directory,
            child: None,
            started: None,
            password: String::new(),
            settings,
            error: None,
            version,
            schema,
        };
        result.validate_options(&result.settings)?;
        Ok(result)
    }

    pub fn endpoint(&self) -> String {
        format!("http://127.0.0.1:{}", self.settings.port)
    }

    pub fn status(&mut self) -> Result<Status, String> {
        if let Some(child) = self.child.as_mut() {
            if let Some(exit) = child.try_wait().map_err(|e| e.to_string())? {
                self.error = Some(format!(
                    "The server exited ({exit}). Check Logs for details."
                ));
                self.child = None;
                self.started = None;
                self.password.clear();
            }
        }
        Ok(Status {
            running: self.child.is_some(),
            pid: self.child.as_ref().map(Child::id),
            port: self.settings.port,
            endpoint: self.endpoint(),
            database: self.database(),
            log_file: self.directory.join("server.log").display().to_string(),
            uptime_seconds: self.started.map(|t| t.elapsed().as_secs()).unwrap_or(0),
            database_bytes: fs::metadata(self.database()).map(|m| m.len()).unwrap_or(0),
            version: self.version.clone(),
            error: self.error.clone(),
            settings: public_settings(&self.settings),
        })
    }

    pub fn start(&mut self) -> Result<Status, String> {
        if self.status()?.running {
            return self.status();
        }
        self.error = None;
        // Refuse a collision; never attach to, authenticate to, or stop somebody else's server.
        let reservation = TcpListener::bind(("127.0.0.1", self.settings.port)).map_err(|_| {
            format!(
                "Port {} is already in use. Choose another port in Settings.",
                self.settings.port
            )
        })?;
        self.password = random_secret()?;
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.directory.join("server.log"))
            .map_err(|e| e.to_string())?;
        let mut command = Command::new(&self.binary);
        command
            .current_dir(&self.directory)
            .args([
                "--host=127.0.0.1",
                &format!("--port={}", self.settings.port),
                "--web-ui=true",
                "--admin-username=hardcharger",
                "--health-port=0",
                "--metrics-port=0",
                "--graceful-shutdown=true",
                "--shutdown-grace-period-seconds=5",
                "--log-emoji=never",
            ])
            .arg(format!("--database={}", self.database()))
            .arg(format!(
                "--config={}",
                self.directory.join("server.yaml").display()
            ))
            .arg(format!(
                "--sensitive-paths={}",
                self.directory.join("settings.json").display()
            ))
            .stdin(Stdio::null())
            .stdout(log.try_clone().map_err(|e| e.to_string())?)
            .stderr(log);
        // A terminal's server configuration must not silently change the app's networking,
        // TLS, credentials, init SQL, or logging. Leave ordinary OS/home variables intact.
        for (key, _) in std::env::vars_os() {
            if key.to_string_lossy().starts_with("HARDCHARGER_") {
                command.env_remove(key);
            }
        }
        command.env("HARDCHARGER_ADMIN_PASSWORD", &self.password);
        let mut options = self.settings.server_options.clone();
        options.remove("database");
        for (name, value) in &self.settings.server_options {
            if is_secret(name) {
                command.env(
                    format!("HARDCHARGER_{}", name.to_uppercase()),
                    value.as_str().ok_or("Secret options must be strings.")?,
                );
                options.remove(name);
            }
        }
        // JSON is a YAML 1.2 document too. Let the existing server read and validate it;
        // there is no second implementation of its configuration precedence or parsing.
        private_write(
            &self.directory.join("server.yaml"),
            &serde_json::to_vec_pretty(&options).map_err(|e| e.to_string())?,
        )?;
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            // A hidden console shared with the child permits targeted Ctrl+Break shutdown.
            unsafe {
                use windows_sys::Win32::{System::Console::*, UI::WindowsAndMessaging::*};
                if GetConsoleWindow().is_null() && AllocConsole() != 0 {
                    ShowWindow(GetConsoleWindow(), SW_HIDE);
                }
            }
            command.creation_flags(0x00000200); // CREATE_NEW_PROCESS_GROUP
        }
        drop(reservation);
        self.child = Some(
            command
                .spawn()
                .map_err(|e| format!("Could not start server: {e}"))?,
        );
        self.started = Some(Instant::now());
        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            if !self.status()?.running {
                return Err(self
                    .error
                    .clone()
                    .unwrap_or("The server stopped during startup.".into()));
            }
            if self.sign_in().is_ok() {
                // Also verify the actual web interface is embedded in this server build.
                let ready = reqwest::blocking::Client::builder()
                    .no_proxy()
                    .timeout(Duration::from_secs(2))
                    .build()
                    .map_err(|e| e.to_string())?
                    .get(format!("{}/ui/", self.endpoint()))
                    .send();
                if ready.is_ok_and(|r| r.status().is_success()) {
                    return self.status();
                }
            }
            if Instant::now() >= deadline {
                self.stop()?;
                return Err("The server did not become ready within 30 seconds. Check Logs and use a build with the web UI embedded.".into());
            }
            std::thread::sleep(Duration::from_millis(150));
        }
    }

    pub fn sign_in(&self) -> Result<serde_json::Value, String> {
        if self.child.is_none() {
            return Err("Start the local server first.".into());
        }
        let client = reqwest::blocking::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(2))
            .build()
            .map_err(|e| e.to_string())?;
        let response = client.post(format!("{}/warp/v1/auth/token", self.endpoint()))
            .json(&serde_json::json!({"grant_type": "password", "username": "hardcharger", "password": self.password}))
            .send().and_then(|r| r.error_for_status()).map_err(|_| "Could not sign in to the local server.".to_string())?;
        let token: serde_json::Value = response.json().map_err(|e| e.to_string())?;
        let access = token["access_token"]
            .as_str()
            .ok_or("The server returned no access token.")?;
        let expires = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|e| e.to_string())?
            .as_millis() as u64
            + token["expires_in"].as_u64().unwrap_or(3600) * 1000;
        Ok(serde_json::json!({"username": "hardcharger", "token": access, "expiresAt": expires}))
    }

    pub fn stop(&mut self) -> Result<Status, String> {
        if let Some(mut child) = self.child.take() {
            if child.try_wait().map_err(|e| e.to_string())?.is_none() {
                let requested = request_stop(child.id());
                if requested.is_err() {
                    self.child = Some(child);
                    return Err(
                        "Could not request a clean server shutdown; the server is still running."
                            .into(),
                    );
                }
                let deadline = Instant::now() + Duration::from_secs(12);
                while child.try_wait().map_err(|e| e.to_string())?.is_none() {
                    if Instant::now() >= deadline {
                        self.child = Some(child);
                        return Err("The server is still stopping. Wait for running queries to finish, then try again.".into());
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
            }
        }
        self.started = None;
        self.password.clear();
        self.error = None;
        self.status()
    }

    pub fn save_settings(&mut self, settings: Settings) -> Result<Status, String> {
        settings.validate()?;
        let mut settings = settings;
        for (name, value) in &mut settings.server_options {
            if value.as_str() == Some(REDACTED) {
                *value = self
                    .settings
                    .server_options
                    .get(name)
                    .ok_or_else(|| format!("No stored value for {name}."))?
                    .clone();
            }
        }
        self.validate_options(&settings)?;
        for (name, value) in &settings.server_options {
            validate_option_choice(name, value)?;
        }
        if self.status()?.running {
            return Err("Stop the server before changing its settings.".into());
        }
        let secrets: BTreeMap<_, _> = settings
            .server_options
            .iter()
            .filter(|(key, _)| is_secret(key))
            .collect();
        if !secrets.is_empty()
            || self
                .settings
                .server_options
                .keys()
                .any(|key| is_secret(key))
        {
            credential_store()?
                .set_password(&serde_json::to_string(&secrets).map_err(|e| e.to_string())?)
                .map_err(|e| {
                    format!("Could not save server credentials in the OS credential store: {e}")
                })?;
        }
        private_write(
            &self.directory.join("settings.json"),
            &serde_json::to_vec_pretty(&public_settings(&settings)).map_err(|e| e.to_string())?,
        )?;
        self.settings = settings;
        self.status()
    }

    pub fn logs(&self) -> Result<String, String> {
        tail(&self.directory.join("server.log"), 128 * 1024)
    }

    pub fn database(&self) -> String {
        let path = self
            .settings
            .server_options
            .get("database")
            .and_then(|v| v.as_str())
            .map(str::to_owned)
            .unwrap_or_else(|| "Hardcharger.duckdb".into());
        if path == ":memory:" || Path::new(&path).is_absolute() {
            path
        } else {
            self.directory.join(path).display().to_string()
        }
    }

    pub fn options(&self) -> Vec<serde_json::Value> {
        self.schema
            .iter()
            .map(|option| {
                let mut option = option.clone();
                let name = option["name"].as_str().unwrap_or("").to_owned();
                option["managed"] = managed(&name).into();
                option["secret"] = is_secret(&name).into();
                let choices = option_choices(&name);
                if !choices.is_empty() {
                    option["choices"] = serde_json::json!(choices);
                    option["choices_ignore_case"] = choices_ignore_case(&name).into();
                }
                option
            })
            .collect()
    }

    fn validate_options(&self, settings: &Settings) -> Result<(), String> {
        for (name, value) in &settings.server_options {
            let option = self
                .schema
                .iter()
                .find(|o| o["name"] == *name)
                .ok_or_else(|| format!("Unknown server option: {name}"))?;
            if managed(name) || name == "port" {
                return Err(format!(
                    "{name} is managed by the desktop app. Remove it from the advanced settings."
                ));
            }
            let valid = match option["type"].as_str() {
                Some("string") => value.is_string(),
                Some("integer") => value.is_i64(),
                Some("boolean") => value.is_boolean(),
                Some("list") => value
                    .as_array()
                    .is_some_and(|a| a.iter().all(|v| v.is_string())),
                _ => false,
            };
            if !valid {
                return Err(format!(
                    "{name} must have type {}.",
                    option["type"].as_str().unwrap_or("unknown")
                ));
            }
        }
        Ok(())
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

fn private_write(destination: &Path, bytes: &[u8]) -> Result<(), String> {
    use std::io::Write;
    let staging = destination.with_extension("pending");
    let mut options = OpenOptions::new();
    options.create(true).write(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
        .open(&staging)
        .and_then(|mut file| {
            file.write_all(bytes)?;
            file.sync_all()
        })
        .map_err(|e| e.to_string())?;
    fs::rename(staging, destination).map_err(|e| e.to_string())
}

#[cfg(unix)]
fn request_stop(pid: u32) -> Result<(), String> {
    if unsafe { libc::kill(pid as i32, libc::SIGTERM) } == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error().to_string())
    }
}

#[cfg(windows)]
fn request_stop(pid: u32) -> Result<(), String> {
    use windows_sys::Win32::System::Console::{GenerateConsoleCtrlEvent, CTRL_BREAK_EVENT};
    if unsafe { GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid) } != 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error().to_string())
    }
}

fn tail(path: &Path, maximum: u64) -> Result<String, String> {
    let mut file = match File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok("The server has not written any logs yet.".into())
        }
        Err(e) => return Err(e.to_string()),
    };
    let size = file.metadata().map_err(|e| e.to_string())?.len();
    file.seek(SeekFrom::Start(size.saturating_sub(maximum)))
        .map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes).map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&bytes);
    Ok(if size > maximum {
        text.split_once('\n')
            .map(|(_, rest)| rest)
            .unwrap_or("")
            .to_owned()
    } else {
        text.into_owned()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixed_choices_match_server_validation() {
        for (name, values) in [
            ("capture_query_profile", vec!["off", "standard", "detailed"]),
            (
                "log_level",
                vec!["debug", "INFO", "warning", "warn", "error"],
            ),
            ("log_format", vec!["text", "json"]),
            ("log_emoji", vec!["auto", "ALWAYS", "never"]),
        ] {
            for value in values {
                assert!(validate_option_choice(name, &value.into()).is_ok());
            }
            assert!(validate_option_choice(name, &"bogus".into()).is_err());
        }
        assert!(validate_option_choice("capture_query_profile", &"Off".into()).is_err());
        assert!(validate_option_choice("log_format", &"JSON".into()).is_err());
        assert!(validate_option_choice("memory_limit", &"8GB".into()).is_ok());
    }

    #[test]
    fn native_settings_never_return_stored_secrets() {
        let mut settings = Settings::default();
        settings
            .server_options
            .insert("oauth_client_secret".into(), "private-test-value".into());
        settings
            .server_options
            .insert("block_unredacted_secrets".into(), true.into());
        let public = public_settings(&settings);
        assert_eq!(public.server_options["oauth_client_secret"], REDACTED);
        assert_eq!(public.server_options["block_unredacted_secrets"], true);
        assert!(!serde_json::to_string(&public)
            .unwrap()
            .contains("private-test-value"));
        assert!(!is_secret("block_unredacted_secrets"));
    }

    #[test]
    fn lifecycle_with_real_server() {
        let Some(binary) = std::env::var_os("HARDCHARGER_DESKTOP_TEST_SERVER") else {
            eprintln!("Set HARDCHARGER_DESKTOP_TEST_SERVER to exercise the real server lifecycle.");
            return;
        };
        let directory = std::env::temp_dir().join(format!(
            "hardcharger-desktop-test-{}",
            random_secret().unwrap()
        ));
        let mut server = Server::new(binary.into(), directory.clone()).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let mut settings = Settings {
            port,
            start_on_open: false,
            ..Settings::default()
        };
        settings
            .server_options
            .insert("memory_limit".into(), "512MB".into());
        settings
            .server_options
            .insert("print_queries".into(), false.into());
        settings
            .server_options
            .insert("capture_query_profile".into(), "detailed".into());
        server.save_settings(settings.clone()).unwrap();
        for (name, value) in [("capture_query_profile", "Off"), ("print_queries", "yes")] {
            let mut invalid = settings.clone();
            invalid.server_options.insert(name.into(), value.into());
            assert!(server.save_settings(invalid).is_err());
            assert_eq!(server.settings.server_options, settings.server_options);
        }
        let mut invalid = settings.clone();
        invalid
            .server_options
            .insert("not_a_setting".into(), true.into());
        assert!(server.save_settings(invalid).is_err());
        let mut invalid = settings.clone();
        invalid
            .server_options
            .insert("host".into(), "0.0.0.0".into());
        assert!(server.save_settings(invalid).is_err());
        assert_eq!(
            Server::new(server.binary.clone(), directory.clone())
                .unwrap()
                .settings
                .server_options,
            settings.server_options
        );
        assert!(server.start().err().unwrap().contains("already in use"));
        assert!(!server.status().unwrap().running);
        drop(listener);
        assert!(server.start().unwrap().running);
        assert!(server.sign_in().unwrap()["token"].as_str().unwrap().len() > 20);
        assert!(server.save_settings(Settings::default()).is_err());
        // A real write survives clean shutdown and restart of the same database.
        let client = server.binary.with_file_name("hardcharger");
        let output = Command::new(&client)
            .args([
                "--host",
                "127.0.0.1",
                "--port",
                &port.to_string(),
                "--no-tls",
                "--username",
                "hardcharger",
                "-c",
                "CREATE TABLE desktop_lifecycle AS SELECT 42 AS answer;",
            ])
            .env("HARDCHARGER_PASSWORD", &server.password)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(!server.stop().unwrap().running);
        assert!(TcpListener::bind(("127.0.0.1", port)).is_ok());
        assert!(server.start().unwrap().running);
        let output = Command::new(client)
            .args([
                "--host",
                "127.0.0.1",
                "--port",
                &port.to_string(),
                "--no-tls",
                "--username",
                "hardcharger",
                "-c",
                "SELECT answer FROM desktop_lifecycle;",
            ])
            .env("HARDCHARGER_PASSWORD", &server.password)
            .output()
            .unwrap();
        assert!(output.status.success());
        assert!(String::from_utf8_lossy(&output.stdout).contains("42"));
        server.stop().unwrap();
        assert!(server.logs().unwrap().contains("Hardcharger is ready"));
        fs::remove_dir_all(directory).unwrap();
    }
}
