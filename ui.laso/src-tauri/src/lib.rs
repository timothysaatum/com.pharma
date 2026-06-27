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
        .plugin(tauri_plugin_sql::Builder::new().build())
        .setup(|app| {
            #[cfg(desktop)]
            app.handle().plugin(tauri_plugin_updater::Builder::new().build())?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            secure_set,
            secure_get,
            secure_delete
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
