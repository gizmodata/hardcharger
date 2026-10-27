//! In-app updates (gizmodata/hardcharger#7): find a newer signed release, show its notes,
//! and only with the user's consent stop the server, install the release and relaunch.
//!
//! The feed is the latest.json a server release publishes (server.yml's release job): one
//! package per platform (the notarized .app.tar.gz, the Authenticode-signed MSIs), each
//! signed with the updater key whose public half is tauri.conf.json's plugins.updater.pubkey.
//! The plugin verifies that signature before it installs anything. Stable users read
//! releases/latest, which is never a release candidate; with prereleases on, the newest
//! release of any kind that has a latest.json. Python package tags (python-v*) have none.

use semver::Version;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter, Manager, Url};
use tauri_plugin_updater::{Update, UpdaterExt};

const STABLE_FEED: &str =
    "https://github.com/gizmodata/hardcharger/releases/latest/download/latest.json";
const RELEASES: &str = "https://api.github.com/repos/gizmodata/hardcharger/releases?per_page=50";
const FEED_FILE: &str = "latest.json";
const FIRST_CHECK: Duration = Duration::from_secs(30);
const CHECK_EVERY: Duration = Duration::from_secs(6 * 60 * 60);
/// A feed other than GitHub's, for testing. Its packages must still carry the updater key's
/// signature: the plugin refuses anything else.
const FEED_OVERRIDE: &str = "HARDCHARGER_DESKTOP_UPDATE_FEED";

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct Preferences {
    /// Check at launch and every six hours.
    pub automatic: bool,
    /// Offer release candidates too.
    pub prereleases: bool,
    /// A version the user chose to skip: no badge for it, though About still shows it.
    pub skipped: Option<String>,
}

impl Default for Preferences {
    fn default() -> Self {
        Self {
            automatic: true,
            prereleases: false,
            skipped: None,
        }
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Available {
    pub version: String,
    pub notes: String,
    pub date: Option<String>,
}

#[derive(Clone, Default, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateState {
    /// This app's version.
    pub current: String,
    pub checking: bool,
    /// When the last check finished, in Unix seconds.
    pub checked_at: Option<u64>,
    pub available: Option<Available>,
    pub installing: bool,
    /// The last check's or install's failure; a later success clears it.
    pub error: Option<String>,
    pub preferences: Preferences,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct Progress {
    downloaded: u64,
    total: Option<u64>,
}

pub struct Updates {
    file: PathBuf,
    state: Mutex<UpdateState>,
    update: Mutex<Option<Update>>,
}

impl Updates {
    pub fn new(directory: &Path, current: String) -> Self {
        let file = directory.join("updates.json");
        let preferences = fs::read(&file)
            .ok()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
            .unwrap_or_default();
        Self {
            file,
            state: Mutex::new(UpdateState {
                current,
                preferences,
                ..Default::default()
            }),
            update: Mutex::new(None),
        }
    }

    pub fn state(&self) -> UpdateState {
        self.state.lock().map(|s| s.clone()).unwrap_or_default()
    }

    fn with<T>(&self, change: impl FnOnce(&mut UpdateState) -> T) -> Result<T, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "The update state is unavailable.")?;
        Ok(change(&mut state))
    }

    pub fn set_preferences(&self, preferences: Preferences) -> Result<UpdateState, String> {
        let bytes = serde_json::to_vec_pretty(&preferences).map_err(|e| e.to_string())?;
        fs::write(&self.file, bytes).map_err(|e| format!("Could not save update settings: {e}"))?;
        self.with(|s| {
            s.preferences = preferences;
            s.clone()
        })
    }
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or_default()
}

fn changed(app: &AppHandle) {
    let _ = app.emit_to("main", "update-changed", ());
}

#[derive(Deserialize)]
pub(crate) struct Release {
    tag_name: String,
    draft: bool,
    assets: Vec<Asset>,
}

#[derive(Deserialize)]
pub(crate) struct Asset {
    name: String,
    browser_download_url: String,
}

/// The feed of the newest release, release candidates included: only this app's own
/// vX.Y.Z[-rc.N] tags (never python-v*), never a draft, and only a release that has one.
/// Versions compare as semver, so 2.10.0 is newer than 2.9.0 and 2.2.0 than 2.2.0-rc.3.
pub(crate) fn newest_feed(releases: &[Release]) -> Option<String> {
    releases
        .iter()
        .filter(|release| !release.draft)
        .filter_map(|release| {
            let version = Version::parse(release.tag_name.strip_prefix('v')?).ok()?;
            let feed = release.assets.iter().find(|a| a.name == FEED_FILE)?;
            Some((version, feed.browser_download_url.clone()))
        })
        .max_by(|a, b| a.0.cmp(&b.0))
        .map(|(_, url)| url)
}

async fn feed(prereleases: bool) -> Result<Option<String>, String> {
    if let Ok(url) = std::env::var(FEED_OVERRIDE) {
        if !url.is_empty() {
            return Ok(Some(url));
        }
    }
    if !prereleases {
        return Ok(Some(STABLE_FEED.into()));
    }
    let agent = format!("Hardcharger-Desktop/{}", env!("CARGO_PKG_VERSION"));
    let releases = tauri::async_runtime::spawn_blocking(move || {
        reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(30))
            .user_agent(agent)
            .build()?
            .get(RELEASES)
            .header("Accept", "application/vnd.github+json")
            .send()?
            .error_for_status()?
            .json::<Vec<Release>>()
    })
    .await
    .map_err(|e| e.to_string())?
    .map_err(|e| format!("Could not list releases: {e}"))?;
    Ok(newest_feed(&releases))
}

async fn find(app: &AppHandle, prereleases: bool) -> Result<Option<Update>, String> {
    let Some(feed) = feed(prereleases).await? else {
        return Ok(None);
    };
    let url = Url::parse(&feed).map_err(|e| e.to_string())?;
    let updater = app
        .updater_builder()
        .endpoints(vec![url])
        .map_err(|e| e.to_string())?
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
    match updater.check().await {
        Ok(update) => Ok(update),
        // A release without a feed (404), or without a package for this platform, offers
        // nothing to install here: not an error the user can act on.
        Err(tauri_plugin_updater::Error::ReleaseNotFound)
        | Err(tauri_plugin_updater::Error::TargetNotFound(_))
        | Err(tauri_plugin_updater::Error::TargetsNotFound(_)) => Ok(None),
        Err(e) => Err(format!("Could not check for updates: {e}")),
    }
}

/// Checks the feed now. Failures are recorded in the state, never raised to the server or
/// the workspace: a check runs beside them and an offline computer simply has no update.
pub async fn check(app: &AppHandle) -> Result<UpdateState, String> {
    let updates = app.state::<Updates>();
    let prereleases = updates.with(|s| {
        if s.checking || s.installing {
            return None;
        }
        s.checking = true;
        Some(s.preferences.prereleases)
    })?;
    let Some(prereleases) = prereleases else {
        return Ok(updates.state());
    };
    changed(app);
    let found = find(app, prereleases).await;
    let state = updates.with(|s| {
        s.checking = false;
        s.checked_at = Some(now());
        match &found {
            Ok(update) => {
                s.error = None;
                s.available = update.as_ref().map(|u| Available {
                    version: u.version.clone(),
                    notes: u.body.clone().unwrap_or_default(),
                    date: u.date.map(|d| d.date().to_string()),
                });
            }
            Err(e) => s.error = Some(e.clone()),
        }
        s.clone()
    })?;
    if let Ok(update) = found {
        if let Ok(mut pending) = updates.update.lock() {
            *pending = update;
        }
    }
    changed(app);
    Ok(state)
}

/// Downloads the update while the server keeps running, then (the caller having the user's
/// consent when the server runs) stops the server the way the Stop button does, installs and
/// relaunches. On Windows the plugin hands over to msiexec and exits; the MSI relaunches.
pub async fn install(app: &AppHandle, server: crate::Shared) -> Result<(), String> {
    let updates = app.state::<Updates>();
    let update = updates
        .update
        .lock()
        .map_err(|_| "The update state is unavailable.")?
        .clone()
        .ok_or("No update is ready to install. Check for updates first.")?;
    updates.with(|s| {
        s.installing = true;
        s.error = None;
    })?;
    changed(app);
    let result = download_and_apply(app, server, &update).await;
    if let Err(e) = &result {
        updates.with(|s| {
            s.installing = false;
            s.error = Some(e.clone());
        })?;
        changed(app);
    }
    result
}

async fn download_and_apply(
    app: &AppHandle,
    server: crate::Shared,
    update: &Update,
) -> Result<(), String> {
    let mut downloaded = 0u64;
    let bytes = update
        .download(
            |chunk, total| {
                downloaded += chunk as u64;
                let _ = app.emit_to("main", "update-progress", Progress { downloaded, total });
            },
            || (),
        )
        .await
        .map_err(|e| format!("Could not download Hardcharger {}: {e}", update.version))?;
    // Stop through the same path as the Stop button: the workspace and query windows close
    // first, then the server drains. A server that won't stop leaves the update uninstalled.
    let running = crate::work(server.clone(), |s| s.status()).await?.running;
    if running {
        crate::control(app, server, "stop").await?;
    }
    update
        .install(bytes)
        .map_err(|e| format!("Could not install Hardcharger {}: {e}", update.version))?;
    app.restart()
}

/// The first automatic check comes shortly after launch, then one every six hours, each only
/// while "Check for updates automatically" is on.
pub fn schedule(app: AppHandle) {
    std::thread::spawn(move || {
        std::thread::sleep(FIRST_CHECK);
        loop {
            if app.state::<Updates>().state().preferences.automatic {
                let _ = tauri::async_runtime::block_on(check(&app));
            }
            std::thread::sleep(CHECK_EVERY);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn release(tag: &str, draft: bool, feed: bool) -> Release {
        let mut assets = vec![Asset {
            name: "Hardcharger-desktop-macos-arm64.dmg".into(),
            browser_download_url: format!("https://example.test/{tag}/dmg"),
        }];
        if feed {
            assets.push(Asset {
                name: FEED_FILE.into(),
                browser_download_url: format!("https://example.test/{tag}/latest.json"),
            });
        }
        Release {
            tag_name: tag.into(),
            draft,
            assets,
        }
    }

    #[test]
    fn newest_feed_compares_semver_and_skips_python_tags_drafts_and_releases_without_a_feed() {
        let releases = [
            release("v2.9.0", false, true),
            release("v2.10.0", false, true),
            release("python-v9.9.9", false, true),
            release("v3.0.0", true, true),
            release("v2.11.0", false, false),
            release("not-a-version", false, true),
        ];
        assert_eq!(
            newest_feed(&releases).as_deref(),
            Some("https://example.test/v2.10.0/latest.json")
        );
    }

    #[test]
    fn newest_feed_orders_release_candidates_before_their_release() {
        let candidates = [
            release("v2.2.0-rc.2", false, true),
            release("v2.1.1", false, true),
        ];
        assert_eq!(
            newest_feed(&candidates).as_deref(),
            Some("https://example.test/v2.2.0-rc.2/latest.json")
        );
        let released = [
            release("v2.2.0-rc.10", false, true),
            release("v2.2.0", false, true),
            release("v2.2.0-rc.9", false, true),
        ];
        assert_eq!(
            newest_feed(&released).as_deref(),
            Some("https://example.test/v2.2.0/latest.json")
        );
        assert_eq!(newest_feed(&[]), None);
    }

    #[test]
    fn preferences_default_to_automatic_stable_checks_and_survive_a_reload() {
        let directory = std::env::temp_dir().join(format!("hc-updates-{}", now_nanos()));
        fs::create_dir_all(&directory).unwrap();
        let updates = Updates::new(&directory, "2.2.0".into());
        assert_eq!(updates.state().preferences, Preferences::default());
        assert!(updates.state().preferences.automatic);
        let chosen = Preferences {
            automatic: false,
            prereleases: true,
            skipped: Some("2.2.1".into()),
        };
        updates.set_preferences(chosen.clone()).unwrap();
        assert_eq!(
            Updates::new(&directory, "2.2.0".into()).state().preferences,
            chosen
        );
        // An unreadable file falls back to the defaults rather than blocking the app.
        fs::write(directory.join("updates.json"), b"{not json").unwrap();
        assert_eq!(
            Updates::new(&directory, "2.2.0".into()).state().preferences,
            Preferences::default()
        );
        fs::remove_dir_all(&directory).unwrap();
    }

    /// The updater only installs packages signed with this key's private half, so a release
    /// built with a placeholder could never update. Tauri writes it as base64 of a minisign
    /// public key file: a comment line, then base64 of 42 bytes ("Ed", key id, Ed25519 key).
    #[test]
    fn the_configured_updater_key_is_a_minisign_public_key() {
        use base64::{engine::general_purpose::STANDARD, Engine};
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let pubkey = config["plugins"]["updater"]["pubkey"].as_str().unwrap();
        let text =
            String::from_utf8(STANDARD.decode(pubkey).expect("pubkey is not base64")).unwrap();
        let mut lines = text.lines();
        assert!(
            lines
                .next()
                .unwrap()
                .starts_with("untrusted comment: minisign public key"),
            "{text}"
        );
        let key = STANDARD.decode(lines.next().unwrap().trim()).unwrap();
        assert_eq!((key.len(), &key[..2]), (42, &b"Ed"[..]));
    }

    fn now_nanos() -> u128 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    }
}
