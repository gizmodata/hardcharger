use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom},
    net::{IpAddr, SocketAddr, TcpListener},
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
    pub network: NetworkSettings,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct NetworkSettings {
    pub enabled: bool,
    pub listen_address: String,
    pub server_name: String,
    pub tls: bool,
    pub tls_cert: String,
    pub tls_key: String,
    pub username: String,
    pub password: String,
}

impl Default for NetworkSettings {
    fn default() -> Self {
        Self {
            enabled: false,
            listen_address: "0.0.0.0".into(),
            server_name: String::new(),
            tls: true,
            tls_cert: String::new(),
            tls_key: String::new(),
            username: "hardcharger".into(),
            password: String::new(),
        }
    }
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            port: 31337,
            start_on_open: true,
            server_options: BTreeMap::new(),
            network: NetworkSettings::default(),
        }
    }
}

impl Settings {
    pub fn validate(&self) -> Result<(), String> {
        if self.port < 1024 {
            return Err("Choose a port between 1024 and 65535.".into());
        }
        let n = &self.network;
        if n.enabled {
            n.listen_address.parse::<IpAddr>().map_err(|_| {
                "Listen address must be an IPv4 or IPv6 address (for example 0.0.0.0)."
            })?;
            let host = n.server_name.trim_matches(['[', ']']);
            if host.is_empty()
                || host
                    .chars()
                    .any(|c| c.is_whitespace() || matches!(c, '/' | '\\' | '@' | '?' | '#'))
                || (host.contains(':') && host.parse::<IpAddr>().is_err())
                || host.parse::<IpAddr>().is_ok_and(|ip| ip.is_unspecified())
            {
                return Err("Server address must be a DNS name or usable IP address, without a scheme, port or path.".into());
            }
            let authority = if host.contains(':') {
                format!("[{host}]")
            } else {
                host.into()
            };
            tauri::Url::parse(&format!("https://{authority}:{}", self.port))
                .map_err(|_| "Server address must be a valid DNS name or IP address.")?;
            if n.username.trim().is_empty() || n.username.chars().any(char::is_control) {
                return Err("Network access requires a user name.".into());
            }
            if n.password.is_empty() {
                return Err("Set a password before allowing network connections.".into());
            }
            if n.tls && (n.tls_cert.is_empty() || n.tls_key.is_empty()) {
                return Err(
                    "TLS requires both a PEM certificate chain and private key path.".into(),
                );
            }
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
const NETWORK_PASSWORD: &str = "__desktop_network_password__";

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
    if !result.network.password.is_empty() {
        result.network.password = REDACTED.into();
    }
    result
}

fn settings_secrets(settings: &Settings) -> BTreeMap<String, serde_json::Value> {
    let mut secrets: BTreeMap<_, _> = settings
        .server_options
        .iter()
        .filter(|(key, _)| is_secret(key))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    if !settings.network.password.is_empty() {
        secrets.insert(
            NETWORK_PASSWORD.into(),
            settings.network.password.clone().into(),
        );
    }
    secrets
}

fn random_secret() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|e| e.to_string())?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

fn request_error(error: reqwest::Error) -> String {
    let mut text = error.to_string();
    let mut cause = std::error::Error::source(&error);
    while let Some(source) = cause {
        text.push_str(": ");
        text.push_str(&source.to_string());
        cause = source.source();
    }
    text
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
        if settings.network.password == REDACTED
            || settings
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
            if settings.network.password == REDACTED {
                settings.network.password = secrets
                    .get(NETWORK_PASSWORD)
                    .and_then(|v| v.as_str())
                    .ok_or("Missing stored network password.")?
                    .into();
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
        if !self.settings.network.enabled {
            return format!("http://127.0.0.1:{}", self.settings.port);
        }
        let host = self.settings.network.server_name.trim_matches(['[', ']']);
        let host = if host.contains(':') {
            format!("[{host}]")
        } else {
            host.into()
        };
        let url = format!(
            "{}://{host}:{}",
            if self.settings.network.tls {
                "https"
            } else {
                "http"
            },
            self.settings.port
        );
        // Match the canonical origin that the webview reports to the token handoff.
        tauri::Url::parse(&url)
            .map(|url| url.origin().ascii_serialization())
            .unwrap_or(url)
    }

    fn listen_address(&self) -> Result<IpAddr, String> {
        if !self.settings.network.enabled {
            return Ok(IpAddr::from([127, 0, 0, 1]));
        }
        self.settings
            .network
            .listen_address
            .parse()
            .map_err(|_| "Invalid listen address.".into())
    }

    fn username(&self) -> &str {
        if self.settings.network.enabled {
            &self.settings.network.username
        } else {
            "hardcharger"
        }
    }

    fn http_client(&self) -> Result<reqwest::blocking::Client, String> {
        let mut builder = reqwest::blocking::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(2));
        if self.settings.network.enabled {
            let listen = self.listen_address()?;
            let control = if listen.is_unspecified() {
                if listen.is_ipv4() {
                    IpAddr::from([127, 0, 0, 1])
                } else {
                    IpAddr::from(std::net::Ipv6Addr::LOCALHOST)
                }
            } else {
                listen
            };
            let endpoint =
                tauri::Url::parse(&self.endpoint()).map_err(|_| "Invalid server address.")?;
            let host = endpoint
                .host_str()
                .ok_or("Missing server address.")?
                .trim_matches(['[', ']']);
            // DNS names keep their TLS identity but control traffic always reaches
            // our own listener, even before the machine's DNS is configured.
            if host.parse::<IpAddr>().is_err() {
                builder = builder.resolve(host, SocketAddr::new(control, self.settings.port));
            } else {
                let address: IpAddr = host.parse().map_err(|_| "Invalid server address.")?;
                if (!listen.is_unspecified() && address != listen)
                    || TcpListener::bind((address, 0)).is_err()
                {
                    return Err("The server IP address must belong to this computer and match the listen address.".into());
                }
            }
            if self.settings.network.tls {
                let pem = fs::read(&self.settings.network.tls_cert)
                    .map_err(|e| format!("Could not read TLS certificate: {e}"))?;
                let certificates = reqwest::Certificate::from_pem_bundle(&pem)
                    .map_err(|e| format!("Invalid PEM certificate chain: {e}"))?;
                if certificates.is_empty() {
                    return Err("The certificate file contains no PEM certificates.".into());
                }
                for cert in certificates {
                    builder = builder.add_root_certificate(cert);
                }
                File::open(&self.settings.network.tls_key)
                    .map_err(|e| format!("Could not read TLS private key: {e}"))?;
            }
        }
        builder.build().map_err(|e| e.to_string())
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
        self.settings.validate()?;
        let client = self.http_client()?;
        // Refuse a collision; never attach to, authenticate to, or stop somebody else's server.
        let reservation =
            TcpListener::bind((self.listen_address()?, self.settings.port)).map_err(|e| {
                if e.kind() == std::io::ErrorKind::AddrInUse {
                    format!(
                        "Port {} is already in use. Choose another port in Settings.",
                        self.settings.port
                    )
                } else {
                    format!(
                        "Could not bind the listen address: {e}. Check Network access in Settings."
                    )
                }
            })?;
        self.password = if self.settings.network.enabled {
            self.settings.network.password.clone()
        } else {
            random_secret()?
        };
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.directory.join("server.log"))
            .map_err(|e| e.to_string())?;
        let mut command = Command::new(&self.binary);
        command
            .current_dir(&self.directory)
            .args([
                &format!("--host={}", self.listen_address()?),
                &format!("--port={}", self.settings.port),
                "--web-ui=true",
                &format!("--admin-username={}", self.username()),
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
        if self.settings.network.enabled && self.settings.network.tls {
            command
                .arg(format!("--tls-cert={}", self.settings.network.tls_cert))
                .arg(format!("--tls-key={}", self.settings.network.tls_key));
        }
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
            let sign_in = self.sign_in_with(&client);
            if sign_in.is_ok() {
                // Also verify the actual web interface is embedded in this server build.
                let ready = client.get(format!("{}/ui/", self.endpoint())).send();
                if ready.is_ok_and(|r| r.status().is_success()) {
                    return self.status();
                }
            }
            if Instant::now() >= deadline {
                self.stop()?;
                return Err(format!("The server did not become ready within 30 seconds. Check Logs, the server address, and TLS certificate names. {}", sign_in.err().unwrap_or_default()));
            }
            std::thread::sleep(Duration::from_millis(150));
        }
    }

    pub fn sign_in(&self) -> Result<serde_json::Value, String> {
        self.sign_in_with(&self.http_client()?)
    }

    fn sign_in_with(
        &self,
        client: &reqwest::blocking::Client,
    ) -> Result<serde_json::Value, String> {
        if self.child.is_none() {
            return Err("Start the local server first.".into());
        }
        let response = client.post(format!("{}/warp/v1/auth/token", self.endpoint()))
            .json(&serde_json::json!({"grant_type": "password", "username": self.username(), "password": self.password}))
            .send().and_then(|r| r.error_for_status()).map_err(|e| format!("Could not sign in to the desktop server: {}", request_error(e)))?;
        let token: serde_json::Value = response.json().map_err(|e| e.to_string())?;
        let access = token["access_token"]
            .as_str()
            .ok_or("The server returned no access token.")?;
        let expires = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|e| e.to_string())?
            .as_millis() as u64
            + token["expires_in"].as_u64().unwrap_or(3600) * 1000;
        Ok(serde_json::json!({"username": self.username(), "token": access, "expiresAt": expires}))
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
        if settings.network.password == REDACTED {
            settings.network.password = self.settings.network.password.clone();
        }
        settings.validate()?;
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
        let secrets = settings_secrets(&settings);
        if !secrets.is_empty() || !settings_secrets(&self.settings).is_empty() {
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
    fn network_defaults_migration_validation_and_secret_redaction() {
        let old: Settings =
            serde_json::from_str(r#"{"port":31337,"start_on_open":true,"server_options":{}}"#)
                .unwrap();
        assert!(!old.network.enabled);
        assert!(old.network.tls);
        let mut settings = old;
        settings.network.enabled = true;
        assert!(settings.validate().is_err());
        settings.network.server_name = "desktop.test".into();
        settings.network.password = "synthetic-network-test-password".into();
        assert!(settings.validate().is_err()); // No certificate/key yet.
        settings.network.tls = false;
        assert!(settings.validate().is_ok());
        let public = public_settings(&settings);
        assert_eq!(public.network.password, REDACTED);
        assert!(!serde_json::to_string(&public)
            .unwrap()
            .contains("synthetic-network-test-password"));
        assert_eq!(
            settings_secrets(&settings)[NETWORK_PASSWORD],
            "synthetic-network-test-password"
        );
        for host in [
            "",
            "0.0.0.0",
            "::",
            "https://desktop.test",
            "user@desktop.test",
            "desktop.test:31337",
            "desktop.test/path",
        ] {
            settings.network.server_name = host.into();
            assert!(settings.validate().is_err(), "{host}");
        }
        settings.network.server_name = "[::1]".into();
        assert!(settings.validate().is_ok());
    }

    #[test]
    fn network_listener_credentials_and_tls_with_real_server() {
        let Some(binary) = std::env::var_os("HARDCHARGER_DESKTOP_TEST_SERVER") else {
            return;
        };
        for tls in [false, true] {
            let directory = std::env::temp_dir().join(format!(
                "hardcharger-network-test-{}",
                random_secret().unwrap()
            ));
            let mut server = Server::new(binary.clone().into(), directory.clone()).unwrap();
            if tls {
                let mut parameters = rcgen::CertificateParams::new(vec![
                    "desktop.test".into(),
                    "localhost".into(),
                    "127.0.0.1".into(),
                ])
                .unwrap();
                parameters.not_before = time::OffsetDateTime::now_utc() - time::Duration::days(1);
                parameters.not_after = time::OffsetDateTime::now_utc() + time::Duration::days(30);
                parameters.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ServerAuth];
                parameters.key_usages = vec![rcgen::KeyUsagePurpose::DigitalSignature];
                let key = rcgen::KeyPair::generate().unwrap();
                let certificate = parameters.self_signed(&key).unwrap();
                fs::write(directory.join("test.pem"), certificate.pem()).unwrap();
                private_write(&directory.join("test.key"), key.serialize_pem().as_bytes()).unwrap();
            }
            let bind = if tls { "127.0.0.1" } else { "::1" };
            let reservation = TcpListener::bind((bind, 0)).unwrap();
            server.settings.port = reservation.local_addr().unwrap().port();
            drop(reservation);
            server.settings.network = NetworkSettings {
                enabled: true,
                listen_address: bind.into(),
                server_name: "desktop.test".into(),
                tls,
                username: "desktop-network-admin".into(),
                password: "synthetic-network-test-password".into(),
                tls_cert: directory.join("test.pem").display().to_string(),
                tls_key: directory.join("test.key").display().to_string(),
            };
            assert!(server.start().unwrap().running);
            let token = server.sign_in().unwrap();
            assert_eq!(token["username"], "desktop-network-admin");
            assert!(token["token"].as_str().unwrap().len() > 20);
            let rejected = server.http_client().unwrap().post(format!("{}/warp/v1/auth/token", server.endpoint()))
                .json(&serde_json::json!({"grant_type":"password", "username":"desktop-network-admin", "password":"wrong"}))
                .send().unwrap();
            assert_eq!(rejected.status(), reqwest::StatusCode::UNAUTHORIZED);
            let config = fs::read_to_string(directory.join("server.yaml")).unwrap();
            assert!(!config.contains("synthetic-network-test-password"));
            if tls {
                server.settings.network.server_name = "wrong-host.test".into();
                assert!(
                    server.sign_in().is_err(),
                    "TLS hostname verification must remain enabled"
                );
                server.settings.network.server_name = "desktop.test".into();
            } else {
                assert!(std::net::TcpStream::connect(("127.0.0.1", server.settings.port)).is_err());
            }
            assert!(!server.stop().unwrap().running);
            assert!(TcpListener::bind((bind, server.settings.port)).is_ok());
            fs::remove_dir_all(directory).unwrap();
        }
    }

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
