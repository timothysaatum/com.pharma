mod db;

use std::io::Write;
use tauri::Manager;

const KEYRING_SERVICE: &str = "com.vermithor.pharmacare";

fn validate_secret_key(key: &str) -> Result<(), String> {
    match key {
        "auth.access_token" | "auth.refresh_token" => Ok(()),
        _ => Err("Unsupported secure storage key".to_string()),
    }
}

fn credential_entry(key: &str) -> Result<keyring::Entry, String> {
    validate_secret_key(key)?;
    keyring::Entry::new(KEYRING_SERVICE, key)
        .map_err(|error| format!("Unable to access the operating-system credential store: {error}"))
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

            // Initialize the local SQLite database with rusqlite + cr-sqlite
            let db_dir = app.path().app_data_dir().ok();
            let resource_dir = app.path().resource_dir().ok();
            let ext_path = match db::resolve_extension_path(resource_dir) {
                Ok(p) => Some(p),
                Err(e) => {
                    eprintln!("[db] {e}");
                    None
                }
            };
            let db_state = db::init_db(db_dir.clone(), ext_path)
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
            db::db_select,
            db::db_execute_batch,
            db::db_test_savepoint,
            db::db_get_crsql_changes,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::validate_secret_key;

    #[test]
    fn secure_storage_only_accepts_auth_token_keys() {
        assert!(validate_secret_key("auth.access_token").is_ok());
        assert!(validate_secret_key("auth.refresh_token").is_ok());
        assert!(validate_secret_key("arbitrary.secret").is_err());
    }
}
