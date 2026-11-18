//! The administrator password of **Only this computer** mode (#56).
//!
//! The server the app starts on 127.0.0.1 has one account, `hardcharger`. Its password is
//! generated once, kept in the operating system's credential store (macOS Keychain, Windows
//! Credential Manager, the Secret Service) under the app's own service name and the account
//! `local-admin-password`, and reused on every start, so other programs on this computer (the
//! CLI, Python/ADBC, JDBC, a browser) can sign in with it. It changes only when the person asks
//! (Generate new password). It never goes into settings.json, server.yaml, Export YAML, the
//! log or a command line: the server gets it in its environment, as network mode's password.
//!
//! When the credential store can't be used (no Secret Service on a Linux desktop, a denied
//! keychain prompt), the server still starts, with a password for this run only, and the
//! dashboard says why it will change at the next start.

use crate::remote_servers::PasswordStore;
use std::sync::Arc;

/// The credential-store account that holds the password.
pub const ACCOUNT: &str = "local-admin-password";
/// The administrator's user name in local-only mode.
pub const USERNAME: &str = "hardcharger";
/// Shortest password accepted from the store (2.14.0's password policy default).
pub const MINIMUM_LENGTH: usize = 12;

/// Letters and digits that can't be mistaken for one another (no 0/O, 1/l/I), so a password
/// read off the screen types back correctly. No character a URI, shell or connection string
/// treats specially.
const ALPHABET: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
const GROUPS: usize = 4;
const GROUP_LENGTH: usize = 5;

/// A new password: four groups of five characters joined by hyphens (23 characters, about
/// 116 bits), with at least one upper-case letter, one lower-case letter and one digit, so it
/// meets any reasonable password policy.
pub fn generate() -> Result<String, String> {
    loop {
        let mut chars = Vec::with_capacity(GROUPS * GROUP_LENGTH);
        while chars.len() < GROUPS * GROUP_LENGTH {
            let mut bytes = [0u8; 64];
            getrandom::fill(&mut bytes).map_err(|e| e.to_string())?;
            // Rejection sampling: no byte value is more likely than another.
            let limit = 256 - 256 % ALPHABET.len();
            for b in bytes {
                if (b as usize) < limit && chars.len() < GROUPS * GROUP_LENGTH {
                    chars.push(ALPHABET[b as usize % ALPHABET.len()] as char);
                }
            }
        }
        let has = |test: fn(&char) -> bool| chars.iter().any(test);
        if !(has(char::is_ascii_uppercase)
            && has(char::is_ascii_lowercase)
            && has(char::is_ascii_digit))
        {
            continue;
        }
        return Ok(chars
            .chunks(GROUP_LENGTH)
            .map(|group| group.iter().collect::<String>())
            .collect::<Vec<_>>()
            .join("-"));
    }
}

/// The password, as the app keeps it between the store and the server.
pub struct LocalAdmin {
    store: Arc<dyn PasswordStore>,
    cached: Option<String>,
    /// Why the store couldn't be used, for the dashboard; None when it works (or wasn't asked yet).
    pub error: Option<String>,
}

impl LocalAdmin {
    pub fn new(store: Arc<dyn PasswordStore>) -> Self {
        Self {
            store,
            cached: None,
            error: None,
        }
    }

    /// The saved password, generating and saving one the first time (also the first start
    /// after an upgrade from a release that had none). Err when the store can't be used.
    pub fn password(&mut self) -> Result<String, String> {
        if let Some(password) = &self.cached {
            return Ok(password.clone());
        }
        let result = match self.store.get(ACCOUNT) {
            Ok(Some(saved)) if saved.chars().count() >= MINIMUM_LENGTH => Ok(saved),
            // None yet, or one too short for the password policy (never written by this app):
            // replace it.
            Ok(_) => generate().and_then(|fresh| self.store.set(ACCOUNT, &fresh).map(|()| fresh)),
            Err(e) => Err(e),
        };
        match result {
            Ok(password) => {
                self.error = None;
                self.cached = Some(password.clone());
                Ok(password)
            }
            Err(e) => {
                self.error = Some(e.clone());
                Err(e)
            }
        }
    }

    /// The password for a start: the saved one, or, when the store can't be used, one for
    /// this run only (self.error then says why).
    pub fn password_for_start(&mut self) -> Result<String, String> {
        match self.password() {
            Ok(password) => Ok(password),
            Err(_) => generate(),
        }
    }

    /// Replaces the saved password with a new one. Nothing changes when it can't be saved.
    pub fn regenerate(&mut self) -> Result<String, String> {
        let fresh = generate()?;
        self.store.set(ACCOUNT, &fresh).map_err(|e| {
            format!("The new password could not be saved in the credential store, so nothing changed: {e}")
        })?;
        self.error = None;
        self.cached = Some(fresh.clone());
        Ok(fresh)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::remote_servers::tests::MemoryStore;

    struct BrokenStore;
    impl PasswordStore for BrokenStore {
        fn get(&self, _: &str) -> Result<Option<String>, String> {
            Err("Could not read the keychain: the user denied access".into())
        }
        fn set(&self, _: &str, _: &str) -> Result<(), String> {
            Err("Could not write to the keychain: the user denied access".into())
        }
        fn delete(&self, _: &str) -> Result<(), String> {
            Ok(())
        }
    }

    #[test]
    fn generated_passwords_meet_the_policy_and_avoid_special_characters() {
        let mut seen = std::collections::BTreeSet::new();
        for _ in 0..2000 {
            let p = generate().unwrap();
            assert_eq!(p.len(), 23, "{p}");
            assert!(p.len() >= MINIMUM_LENGTH);
            assert_eq!(p.matches('-').count(), 3, "{p}");
            assert!(p.chars().any(|c| c.is_ascii_uppercase()), "{p}");
            assert!(p.chars().any(|c| c.is_ascii_lowercase()), "{p}");
            assert!(p.chars().any(|c| c.is_ascii_digit()), "{p}");
            assert!(
                p.chars().all(|c| c == '-' || ALPHABET.contains(&(c as u8))),
                "{p}"
            );
            assert!(
                !p.chars().any(|c| ":@/;?#%&=+ \t'\"\\$`".contains(c)),
                "{p}"
            );
            seen.insert(p);
        }
        assert_eq!(seen.len(), 2000, "every password is new");
    }

    #[test]
    fn generated_once_then_reused_until_regenerated() {
        let store = Arc::new(MemoryStore::default());
        let mut first = LocalAdmin::new(store.clone());
        let password = first.password().unwrap();
        assert_eq!(
            store.get(ACCOUNT).unwrap().as_deref(),
            Some(password.as_str())
        );
        assert_eq!(first.password().unwrap(), password);
        // A relaunch: a new LocalAdmin over the same store reads it back.
        let mut relaunched = LocalAdmin::new(store.clone());
        assert_eq!(relaunched.password().unwrap(), password);
        let fresh = relaunched.regenerate().unwrap();
        assert_ne!(fresh, password);
        assert_eq!(relaunched.password().unwrap(), fresh);
        assert_eq!(LocalAdmin::new(store.clone()).password().unwrap(), fresh);
    }

    #[test]
    fn a_saved_value_too_short_for_the_policy_is_replaced() {
        let store = Arc::new(MemoryStore::default());
        store.set(ACCOUNT, "short").unwrap();
        let password = LocalAdmin::new(store.clone()).password().unwrap();
        assert!(password.len() >= MINIMUM_LENGTH);
        assert_eq!(
            store.get(ACCOUNT).unwrap().as_deref(),
            Some(password.as_str())
        );
    }

    #[test]
    fn without_a_credential_store_a_start_still_gets_a_password_and_says_why() {
        let mut admin = LocalAdmin::new(Arc::new(BrokenStore));
        assert!(admin.password().is_err());
        let one = admin.password_for_start().unwrap();
        assert!(one.len() >= MINIMUM_LENGTH);
        assert!(admin.error.as_deref().unwrap().contains("denied access"));
        assert_ne!(
            admin.password_for_start().unwrap(),
            one,
            "a run-only password, not saved"
        );
        let refused = admin.regenerate().unwrap_err();
        assert!(refused.contains("nothing changed"), "{refused}");
    }

    /// The real credential store, under a throwaway service name (never the app's own), when
    /// HARDCHARGER_DESKTOP_TEST_KEYCHAIN=1 allows the test to write to it.
    #[test]
    fn the_real_credential_store_keeps_the_password_across_a_relaunch() {
        if std::env::var("HARDCHARGER_DESKTOP_TEST_KEYCHAIN").as_deref() != Ok("1") {
            eprintln!("Set HARDCHARGER_DESKTOP_TEST_KEYCHAIN=1 to use the OS credential store (a throwaway service name).");
            return;
        }
        let service = format!(
            "com.gizmodata.hardcharger.desktop.test-{}",
            crate::server::random_secret().unwrap()
        );
        let store = Arc::new(crate::remote_servers::ServiceKeychain(service.clone()));
        let password = LocalAdmin::new(store.clone()).password().unwrap();
        assert_eq!(LocalAdmin::new(store.clone()).password().unwrap(), password);
        let fresh = LocalAdmin::new(store.clone()).regenerate().unwrap();
        assert_eq!(LocalAdmin::new(store.clone()).password().unwrap(), fresh);
        store.delete(ACCOUNT).unwrap();
        assert_eq!(store.get(ACCOUNT).unwrap(), None);
    }
}
