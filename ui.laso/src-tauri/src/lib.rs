mod db;

use std::io::Write;
use tauri::Manager;

const KEYRING_SERVICE: &str = "com.vermithor.pharmacare";

/// Keyring entry name for the local SQLite (SQLCipher) database encryption
/// key. Not exposed via the `secure_get`/`secure_set` IPC commands — see
/// `get_or_create_db_encryption_key` below for why.
const DB_ENCRYPTION_KEY_ID: &str = "db.sqlcipher_key";

fn validate_secret_key(key: &str) -> Result<(), String> {
    match key {
        "auth.access_token" | "auth.refresh_token" | DB_ENCRYPTION_KEY_ID => Ok(()),
        _ => Err("Unsupported secure storage key".to_string()),
    }
}

fn credential_entry(key: &str) -> Result<keyring::Entry, String> {
    validate_secret_key(key)?;
    keyring::Entry::new(KEYRING_SERVICE, key)
        .map_err(|error| format!("Unable to access the operating-system credential store: {error}"))
}

/// Thin seam over "get/set a single secret" so the branching logic in
/// `get_or_create_key_from_store` can be unit tested without touching the
/// real OS keyring (which, depending on the desktop session, can hang or
/// require an interactive unlock — not something a test run should risk).
trait SecretStore {
    fn get(&self) -> Result<Option<String>, String>;
    fn set(&self, value: &str) -> Result<(), String>;
}

impl SecretStore for keyring::Entry {
    fn get(&self) -> Result<Option<String>, String> {
        match self.get_password() {
            Ok(value) => Ok(Some(value)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(error) => Err(format!("Unable to read credential: {error}")),
        }
    }

    fn set(&self, value: &str) -> Result<(), String> {
        self.set_password(value)
            .map_err(|error| format!("Unable to store credential: {error}"))
    }
}

/// If a DB encryption key is already stored, return it unchanged. Otherwise
/// generate a fresh one and store it. Never derives the key from anything
/// user-typed.
fn get_or_create_key_from_store<S: SecretStore>(store: &S) -> Result<String, String> {
    if let Some(existing) = store.get()? {
        return Ok(existing);
    }
    let key = db::generate_sqlcipher_raw_key();
    store.set(&key)?;
    Ok(key)
}

/// How long to wait for the OS credential store before giving up. This runs
/// on the app-startup critical path (see `get_or_create_db_encryption_key`),
/// so an unresponsive keyring/secret-service daemon must fail fast with a
/// diagnosable error rather than hang the app forever with no UI and no log.
const DB_ENCRYPTION_KEY_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);

/// Runs `f` on a background thread and waits up to `timeout` for it to
/// finish. `keyring::Entry` calls can block indefinitely on some machines
/// (no secret-service/keyring daemon running, a locked session, etc.) — this
/// turns that into a bounded, clearly-diagnosable failure instead of an
/// unbounded hang. The spawned thread is not cancelled on timeout; it is
/// abandoned and dies with the process when `.setup()` returns an `Err`
/// (which aborts startup via the `.expect(...)` in `run()`).
fn with_timeout<F>(timeout: std::time::Duration, f: F) -> Result<String, String>
where
    F: FnOnce() -> Result<String, String> + Send + 'static,
{
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(f());
    });
    match rx.recv_timeout(timeout) {
        Ok(result) => result,
        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => Err(format!(
            "Timed out after {timeout:?} waiting for the operating-system credential \
             store to respond while resolving the local database encryption key. The \
             credential store service (keyring/secret-service daemon) may be \
             unavailable, locked, or unresponsive on this machine."
        )),
        Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => Err(
            "The database encryption key lookup thread terminated unexpectedly."
                .to_string(),
        ),
    }
}

/// Resolves the local database's SQLCipher encryption key.
///
/// `db::init_db` runs synchronously inside `.setup()`, before
/// `invoke_handler` registers any commands and before JS has started — so
/// this cannot go through the async `secure_get`/`secure_set` IPC commands.
/// It opens a `keyring::Entry` directly instead, via the same
/// `credential_entry` construction the IPC commands use, bounded by
/// `DB_ENCRYPTION_KEY_TIMEOUT` so a stuck credential store can't hang
/// startup forever.
fn get_or_create_db_encryption_key() -> Result<String, String> {
    with_timeout(DB_ENCRYPTION_KEY_TIMEOUT, || {
        let entry = credential_entry(DB_ENCRYPTION_KEY_ID)?;
        get_or_create_key_from_store(&entry)
    })
}

#[tauri::command]
async fn secure_set(key: String, value: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        credential_entry(&key)?
            .set_password(&value)
            .map_err(|error| format!("Unable to store credential: {error}"))
    })
    .await
    .map_err(|error| format!("Credential task failed: {error}"))?
}

#[tauri::command]
async fn secure_get(key: String) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let entry = credential_entry(&key)?;
        match entry.get_password() {
            Ok(value) => Ok(Some(value)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(error) => Err(format!("Unable to read credential: {error}")),
        }
    })
    .await
    .map_err(|error| format!("Credential task failed: {error}"))?
}

#[tauri::command]
async fn secure_delete(key: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let entry = credential_entry(&key)?;
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(error) => Err(format!("Unable to delete credential: {error}")),
        }
    })
    .await
    .map_err(|error| format!("Credential task failed: {error}"))?
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            #[cfg(desktop)]
            app.handle()
                .plugin(tauri_plugin_updater::Builder::new().build())?;

            // Initialize the local SQLite database with SQLCipher encryption
            let db_dir = app.path().app_data_dir().ok();
            let encryption_key = get_or_create_db_encryption_key().map_err(|error| {
                format!("Failed to obtain local database encryption key: {error}")
            })?;
            let db_state = db::init_db(db_dir.clone(), &encryption_key)
                .map_err(|error| format!("Failed to initialize local database: {error}"))?;
            if let Some(warning) = &db_state.startup_warning {
                eprintln!("[db] {warning}");
                // Release builds on Windows have no console. Preserve the
                // reason in the app data directory so startup failures can be
                // diagnosed on the affected machine.
                if let Some(dir) = db_dir {
                    let _ = std::fs::create_dir_all(&dir);
                    if let Ok(mut file) = std::fs::OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(dir.join("startup.log"))
                    {
                        let _ = writeln!(file, "[db] {warning}");
                    }
                }
            }
            app.manage(db_state);

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            secure_set,
            secure_get,
            secure_delete,
            db::db_execute,
            db::db_execute_transaction,
            db::db_select,
            db::db_execute_batch,
            db::db_test_savepoint,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::{get_or_create_key_from_store, validate_secret_key, with_timeout, SecretStore};
    use std::sync::Mutex;
    use std::time::Duration;

    #[test]
    fn secure_storage_only_accepts_auth_token_keys() {
        assert!(validate_secret_key("auth.access_token").is_ok());
        assert!(validate_secret_key("auth.refresh_token").is_ok());
        assert!(validate_secret_key("db.sqlcipher_key").is_ok());
        assert!(validate_secret_key("arbitrary.secret").is_err());
    }

    /// In-memory stand-in for a `keyring::Entry`, used so the DB-encryption
    /// key round-trip logic can be tested without touching the real OS
    /// keyring (no existing test in this codebase touches the real keyring
    /// either — see `credential_entry`'s only callers, the `secure_*`
    /// commands, which have no test coverage of their own).
    #[derive(Default)]
    struct FakeSecretStore {
        value: Mutex<Option<String>>,
    }

    impl SecretStore for FakeSecretStore {
        fn get(&self) -> Result<Option<String>, String> {
            Ok(self.value.lock().unwrap().clone())
        }

        fn set(&self, value: &str) -> Result<(), String> {
            *self.value.lock().unwrap() = Some(value.to_string());
            Ok(())
        }
    }

    #[test]
    fn db_encryption_key_is_generated_once_and_reused_thereafter() {
        let store = FakeSecretStore::default();

        let first = get_or_create_key_from_store(&store).expect("first call should generate a key");
        assert!(
            first.starts_with("x'") && first.ends_with('\''),
            "should be SQLCipher raw-key format: {first}"
        );

        let second =
            get_or_create_key_from_store(&store).expect("second call should return the same key");
        assert_eq!(first, second, "an existing key must never be regenerated");
    }

    #[test]
    fn db_encryption_key_generation_never_touches_a_populated_store() {
        let store = FakeSecretStore::default();
        *store.value.lock().unwrap() = Some("x'preexisting'".to_string());

        let key = get_or_create_key_from_store(&store).expect("should read the existing key");
        assert_eq!(key, "x'preexisting'");
    }

    #[test]
    fn with_timeout_returns_the_value_when_the_work_finishes_in_time() {
        let result = with_timeout(Duration::from_secs(5), || Ok("done".to_string()));
        assert_eq!(result, Ok("done".to_string()));
    }

    #[test]
    fn with_timeout_propagates_an_error_from_the_work_when_it_finishes_in_time() {
        let result: Result<String, String> =
            with_timeout(Duration::from_secs(5), || Err("boom".to_string()));
        assert_eq!(result, Err("boom".to_string()));
    }

    #[test]
    fn with_timeout_fails_fast_instead_of_hanging_when_the_work_never_returns() {
        // Simulates a stuck keyring/secret-service daemon: the closure never
        // completes. This must return a clear error well before the
        // closure's own delay, proving the caller doesn't hang waiting on it.
        let start = std::time::Instant::now();
        let result = with_timeout(Duration::from_millis(100), || {
            std::thread::sleep(Duration::from_secs(30));
            Ok("too late".to_string())
        });
        let elapsed = start.elapsed();

        assert!(result.is_err(), "expected a timeout error, got {result:?}");
        assert!(
            result.unwrap_err().contains("Timed out"),
            "error message should explain it was a timeout"
        );
        assert!(
            elapsed < Duration::from_secs(2),
            "with_timeout should return promptly on timeout, took {elapsed:?}"
        );
    }
}
