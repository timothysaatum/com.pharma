use base64::{engine::general_purpose::STANDARD as BASE64_STANDARD, Engine as _};
use rusqlite::{params_from_iter, Connection, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;
use std::path::PathBuf;
use std::sync::Mutex;
use tauri::State;

pub struct DbState {
    pub conn: Mutex<Connection>,
    /// Reserved for startup diagnostics. CR-SQLite is required for offline
    /// sync, so initialization now returns an error instead of degrading.
    pub startup_warning: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct ExecResult {
    pub rows_affected: usize,
    pub last_insert_id: Option<i64>,
}

#[derive(Debug, Deserialize)]
pub struct TransactionStatement {
    pub sql: String,
    #[serde(default)]
    pub values: Vec<JsonValue>,
    pub expected_rows: Option<usize>,
    pub error_message: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct DbError {
    pub message: String,
}

impl From<rusqlite::Error> for DbError {
    fn from(e: rusqlite::Error) -> Self {
        DbError {
            message: e.to_string(),
        }
    }
}

fn crsqlite_library_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "crsqlite.dll"
    } else if cfg!(target_os = "macos") {
        "crsqlite.dylib"
    } else {
        "crsqlite.so"
    }
}

fn crsqlite_platform_dir() -> Result<&'static str, String> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("linux", "x86_64") => Ok("linux-x86_64"),
        ("linux", "aarch64") => Ok("linux-aarch64"),
        ("windows", "x86_64") => Ok("windows-x86_64"),
        ("macos", "aarch64") => Ok("darwin-aarch64"),
        ("macos", "x86_64") => Ok("darwin-x86_64"),
        (os, arch) => Err(format!(
            "cr-sqlite extension is not packaged for {os}/{arch}"
        )),
    }
}

fn extension_load_path(ext_path: &std::path::Path) -> PathBuf {
    let p = ext_path.to_string_lossy().to_string();
    let stripped = p
        .strip_suffix(".so")
        .or_else(|| p.strip_suffix(".dylib"))
        .or_else(|| p.strip_suffix(".dll"))
        .unwrap_or(&p);
    PathBuf::from(stripped)
}

/// Resolve the cr-sqlite shared-library path at runtime.
///
/// Resolution order (first match wins):
///   1. `CRSQLITE_EXTENSION_PATH` environment variable
///   2. Tauri resource directory — for production bundling
///   3. Repository-relative layouts — works with `cargo test` and `tauri dev`
///   4. Current/parent directory fallbacks using the explicit architecture dir
pub fn resolve_extension_path(resource_dir: Option<PathBuf>) -> Result<PathBuf, String> {
    // 1. Environment variable (highest priority)
    if let Ok(val) = std::env::var("CRSQLITE_EXTENSION_PATH") {
        let p = PathBuf::from(&val);
        if p.exists() {
            return Ok(p);
        }
    }

    let library_name = crsqlite_library_name();
    let platform_dir = crsqlite_platform_dir()?;

    // Build candidate list
    let mut candidates: Vec<PathBuf> = Vec::new();

    // 2. Tauri resource directory (used in production bundles)
    if let Some(dir) = &resource_dir {
        candidates.push(dir.join("crsqlite").join(platform_dir).join(library_name));
        candidates.push(dir.join(platform_dir).join(library_name));
    }

    // 3. Working-directory fallbacks for repo root, ui.laso, and src-tauri.
    candidates.push(
        PathBuf::from("crsqlite")
            .join(platform_dir)
            .join(library_name),
    );
    candidates.push(
        PathBuf::from("..")
            .join("crsqlite")
            .join(platform_dir)
            .join(library_name),
    );
    candidates.push(
        PathBuf::from("..")
            .join("..")
            .join("crsqlite")
            .join(platform_dir)
            .join(library_name),
    );

    for p in &candidates {
        if p.exists() {
            return Ok(p.clone());
        }
    }

    Err(format!(
        "cr-sqlite extension not found. Tried: env CRSQLITE_EXTENSION_PATH, {candidates:?}"
    ))
}

pub fn init_db(db_dir: Option<PathBuf>, ext_path: Option<PathBuf>) -> Result<DbState, String> {
    // Resolve DB path: use app's data dir or fallback to CWD
    let db_dir = db_dir.unwrap_or_else(|| PathBuf::from("."));
    std::fs::create_dir_all(&db_dir).map_err(|e| format!("Cannot create DB dir: {e}"))?;

    let db_path = db_dir.join("laso.db");
    let conn = Connection::open(&db_path).map_err(|e| format!("Cannot open SQLite DB: {e}"))?;

    // Enable WAL mode
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")
        .map_err(|e| format!("Cannot set pragmas: {e}"))?;

    let ext_path = ext_path.ok_or_else(|| {
        "CR-SQLite extension is required for offline synchronization but was not found".to_string()
    })?;

    // SAFETY: cr-sqlite is a trusted extension shipped with the app.
    // SQLite automatically appends the platform extension suffix
    // (.so on Linux, .dylib on macOS, .dll on Windows), regardless of
    // whether the path already includes it. Strip it to avoid double
    // suffixes (e.g. crsqlite.so.so).
    let ext_load_path = extension_load_path(&ext_path);
    unsafe {
        conn.load_extension(&ext_load_path, None).map_err(|error| {
            format!(
                "Cannot load cr-sqlite extension {}: {error}",
                ext_path.display()
            )
        })?;
    }

    let site_id = conn
        .query_row("SELECT crsql_site_id()", [], |row| row.get::<_, Vec<u8>>(0))
        .map_err(|error| format!("cr-sqlite loaded but crsql_site_id() failed: {error}"))?;
    let site_id_hex: String = site_id.iter().map(|byte| format!("{byte:02x}")).collect();
    println!("[db] cr-sqlite loaded, site_id={site_id_hex}");

    Ok(DbState {
        conn: Mutex::new(conn),
        startup_warning: None,
    })
}

#[cfg(test)]
mod startup_tests {
    use super::{
        crsqlite_platform_dir, execute_transaction, init_db, json_to_rusqlite,
        resolve_extension_path, TransactionStatement,
    };
    use rusqlite::{types::Value, Connection};
    use std::path::Path;

    #[test]
    fn real_crsqlite_extension_loads_without_degraded_warning() {
        let expected_platform_dir = crsqlite_platform_dir().expect("supported test architecture");
        let extension =
            resolve_extension_path(None).expect("test requires a packaged cr-sqlite extension");
        assert!(extension.exists(), "test requires {}", extension.display());
        assert!(
            extension.to_string_lossy().contains(expected_platform_dir),
            "{} should use {expected_platform_dir}",
            extension.display(),
        );
        assert!(
            extension_matches_current_arch(&extension),
            "{} must match the current target architecture",
            extension.display(),
        );

        let temp =
            std::env::temp_dir().join(format!("pharmacare-valid-extension-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&temp);

        let state = init_db(Some(temp.clone()), Some(extension))
            .expect("real cr-sqlite extension should initialize");
        assert_eq!(state.startup_warning, None);
        let site_id: Vec<u8> = state
            .conn
            .lock()
            .unwrap()
            .query_row("SELECT crsql_site_id()", [], |row| row.get(0))
            .unwrap();
        assert!(!site_id.is_empty());

        drop(state);
        let _ = std::fs::remove_dir_all(temp);
    }

    #[test]
    fn invalid_extension_fails_desktop_startup() {
        let temp = std::env::temp_dir().join(format!(
            "pharmacare-invalid-extension-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&temp);
        std::fs::create_dir_all(&temp).unwrap();
        let invalid = temp.join("crsqlite.invalid");
        std::fs::write(&invalid, b"not a shared library").unwrap();

        let err = init_db(Some(temp.clone()), Some(invalid))
            .err()
            .expect("invalid cr-sqlite extension must fail startup");
        assert!(err.contains("Cannot load cr-sqlite extension"));

        let _ = std::fs::remove_dir_all(temp);
    }

    #[test]
    fn missing_extension_fails_desktop_startup() {
        let temp = std::env::temp_dir().join(format!(
            "pharmacare-missing-extension-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&temp);

        let err = init_db(Some(temp.clone()), None)
            .err()
            .expect("missing cr-sqlite extension must fail startup");
        assert!(err.contains("CR-SQLite extension is required"));

        let _ = std::fs::remove_dir_all(temp);
    }

    #[test]
    fn json_blob_transport_marker_decodes_to_sqlite_blob() {
        let value = json_to_rusqlite(serde_json::json!({
            "__laso_blob_b64": "AQIDBA=="
        }))
        .expect("valid blob transport should decode");

        assert_eq!(value, Value::Blob(vec![1, 2, 3, 4]));
    }

    #[test]
    fn blob_transport_does_not_decode_b64_prefixed_plain_strings() {
        let value = json_to_rusqlite(serde_json::json!("b64:AQIDBA=="))
            .expect("plain string should remain text");

        assert_eq!(value, Value::Text("b64:AQIDBA==".to_string()));
    }

    #[test]
    fn invalid_json_blob_transport_marker_is_rejected() {
        let err = json_to_rusqlite(serde_json::json!({
            "__laso_blob_b64": "not valid base64"
        }))
        .err()
        .expect("invalid blob transport should fail");

        assert!(err.message.contains("Invalid SQLite blob transport value"));
    }

    #[test]
    fn guarded_transaction_commits_all_statements() {
        let mut conn = Connection::open_in_memory().unwrap();
        conn.execute(
            "CREATE TABLE inventory (id TEXT PRIMARY KEY, quantity INTEGER)",
            [],
        )
        .unwrap();
        conn.execute("INSERT INTO inventory VALUES ('drug-1', 5)", [])
            .unwrap();

        execute_transaction(
            &mut conn,
            vec![
                TransactionStatement {
                    sql: "UPDATE inventory SET quantity = quantity - ?1 WHERE id = ?2 AND quantity >= ?3".into(),
                    values: vec![serde_json::json!(2), serde_json::json!("drug-1"), serde_json::json!(2)],
                    expected_rows: Some(1),
                    error_message: Some("insufficient stock".into()),
                },
                TransactionStatement {
                    sql: "INSERT INTO inventory VALUES (?1, ?2)".into(),
                    values: vec![serde_json::json!("drug-2"), serde_json::json!(9)],
                    expected_rows: Some(1),
                    error_message: None,
                },
            ],
        )
        .unwrap();

        let rows: Vec<(String, i64)> = conn
            .prepare("SELECT id, quantity FROM inventory ORDER BY id")
            .unwrap()
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
            .unwrap()
            .map(Result::unwrap)
            .collect();
        assert_eq!(rows, vec![("drug-1".into(), 3), ("drug-2".into(), 9)]);
    }

    #[test]
    fn guarded_transaction_rolls_back_every_statement_on_mismatch() {
        let mut conn = Connection::open_in_memory().unwrap();
        conn.execute(
            "CREATE TABLE inventory (id TEXT PRIMARY KEY, quantity INTEGER)",
            [],
        )
        .unwrap();
        conn.execute("INSERT INTO inventory VALUES ('drug-1', 1)", [])
            .unwrap();

        let error = execute_transaction(
            &mut conn,
            vec![
                TransactionStatement {
                    sql: "INSERT INTO inventory VALUES (?1, ?2)".into(),
                    values: vec![serde_json::json!("transient"), serde_json::json!(4)],
                    expected_rows: Some(1),
                    error_message: None,
                },
                TransactionStatement {
                    sql: "UPDATE inventory SET quantity = quantity - ?1 WHERE id = ?2 AND quantity >= ?1".into(),
                    values: vec![serde_json::json!(2), serde_json::json!("drug-1")],
                    expected_rows: Some(1),
                    error_message: Some("Insufficient local stock for drug drug-1".into()),
                },
            ],
        )
        .unwrap_err();

        assert_eq!(error.message, "Insufficient local stock for drug drug-1");
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM inventory", [], |row| row.get(0))
            .unwrap();
        let quantity: i64 = conn
            .query_row(
                "SELECT quantity FROM inventory WHERE id = 'drug-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
        assert_eq!(quantity, 1);
    }

    fn extension_matches_current_arch(path: &Path) -> bool {
        let Ok(bytes) = std::fs::read(path) else {
            return false;
        };
        if bytes.len() < 20 || &bytes[0..4] != b"\x7fELF" {
            return true;
        }
        let machine = u16::from_le_bytes([bytes[18], bytes[19]]);
        match std::env::consts::ARCH {
            "x86_64" => machine == 62,
            "aarch64" => machine == 183,
            _ => true,
        }
    }
}

/// Execute a write query (INSERT/UPDATE/DELETE/CREATE).
/// Returns rows affected and last insert id (if any).
#[tauri::command]
pub fn db_execute(
    db: State<DbState>,
    sql: String,
    values: Vec<JsonValue>,
) -> Result<ExecResult, DbError> {
    let conn = db.conn.lock().map_err(|e| DbError {
        message: format!("Lock error: {e}"),
    })?;

    let params: Vec<rusqlite::types::Value> = values
        .into_iter()
        .map(json_to_rusqlite)
        .collect::<Result<_, _>>()?;

    let mut stmt = conn.prepare(&sql)?;
    let rows_affected = stmt.execute(params_from_iter(params.iter()))?;
    let last_insert_id = conn.last_insert_rowid();
    let last_insert_id = if last_insert_id == 0 {
        None
    } else {
        Some(last_insert_id)
    };

    Ok(ExecResult {
        rows_affected,
        last_insert_id,
    })
}

/// Execute parameterized statements as one immediate SQLite transaction.
///
/// `expected_rows` turns optimistic checks such as stock availability into a
/// transaction guard. A mismatch aborts and rolls back every preceding write.
#[tauri::command]
pub fn db_execute_transaction(
    db: State<DbState>,
    statements: Vec<TransactionStatement>,
) -> Result<Vec<ExecResult>, DbError> {
    let mut conn = db.conn.lock().map_err(|e| DbError {
        message: format!("Lock error: {e}"),
    })?;

    execute_transaction(&mut conn, statements)
}

fn execute_transaction(
    conn: &mut Connection,
    statements: Vec<TransactionStatement>,
) -> Result<Vec<ExecResult>, DbError> {
    let transaction = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut results = Vec::with_capacity(statements.len());

    for statement in statements {
        let params: Vec<rusqlite::types::Value> = statement
            .values
            .into_iter()
            .map(json_to_rusqlite)
            .collect::<Result<_, _>>()?;
        let rows_affected = transaction.execute(&statement.sql, params_from_iter(params.iter()))?;

        if let Some(expected_rows) = statement.expected_rows {
            if rows_affected != expected_rows {
                return Err(DbError {
                    message: statement.error_message.unwrap_or_else(|| {
                        format!(
                            "Transaction guard expected {expected_rows} affected row(s), got {rows_affected}"
                        )
                    }),
                });
            }
        }

        let last_insert_id = transaction.last_insert_rowid();
        results.push(ExecResult {
            rows_affected,
            last_insert_id: (last_insert_id != 0).then_some(last_insert_id),
        });
    }

    transaction.commit()?;
    Ok(results)
}

/// Execute a read query (SELECT) and return rows as JSON objects
/// keyed by column name (matching the old tauri-plugin-sql format).
#[tauri::command]
pub fn db_select(
    db: State<DbState>,
    sql: String,
    values: Vec<JsonValue>,
) -> Result<Vec<serde_json::Map<String, JsonValue>>, DbError> {
    let conn = db.conn.lock().map_err(|e| DbError {
        message: format!("Lock error: {e}"),
    })?;

    let params: Vec<rusqlite::types::Value> = values
        .into_iter()
        .map(json_to_rusqlite)
        .collect::<Result<_, _>>()?;

    let mut stmt = conn.prepare(&sql)?;
    let col_count = stmt.column_count();
    // Get column names
    let col_names: Vec<String> = (0..col_count)
        .map(|i| stmt.column_name(i).unwrap_or("?").to_string())
        .collect();

    let rows = stmt
        .query_map(params_from_iter(params.iter()), |row| {
            let mut obj = serde_json::Map::with_capacity(col_count);
            for i in 0..col_count {
                obj.insert(col_names[i].clone(), row_to_json(row, i));
            }
            Ok(obj)
        })
        .map_err(|e| DbError {
            message: format!("Query error: {e}"),
        })?;

    let mut result = Vec::new();
    for row in rows {
        result.push(row.map_err(|e| DbError {
            message: format!("Row read error: {e}"),
        })?);
    }
    Ok(result)
}

/// Execute a raw batch of SQL (no parameters). Used for migrations.
#[tauri::command]
pub fn db_execute_batch(db: State<DbState>, sql: String) -> Result<(), DbError> {
    let conn = db.conn.lock().map_err(|e| DbError {
        message: format!("Lock error: {e}"),
    })?;
    conn.execute_batch(&sql).map_err(|e| DbError {
        message: format!("Batch execute error: {e}"),
    })
}

/// Run a savepoint-wrapped operation for testing cr-sqlite compatibility.
/// SQL is executed inside a savepoint, then the savepoint is either released
/// or rolled back based on `should_commit`.
#[tauri::command]
pub fn db_test_savepoint(
    db: State<DbState>,
    sql: String,
    should_commit: bool,
) -> Result<ExecResult, DbError> {
    let conn = db.conn.lock().map_err(|e| DbError {
        message: format!("Lock error: {e}"),
    })?;

    conn.execute_batch("SAVEPOINT spike_test")?;
    let rows_affected = conn.execute(&sql, [])?;
    let last_insert_id = conn.last_insert_rowid();

    if should_commit {
        conn.execute_batch("RELEASE spike_test")?;
    } else {
        conn.execute_batch("ROLLBACK TO spike_test")?;
        conn.execute_batch("RELEASE spike_test")?; // release after rollback
    }

    Ok(ExecResult {
        rows_affected,
        last_insert_id: if last_insert_id == 0 {
            None
        } else {
            Some(last_insert_id)
        },
    })
}

/// Check crsql_changes table for entries (for testing)
#[tauri::command]
pub fn db_get_crsql_changes(
    db: State<DbState>,
) -> Result<Vec<serde_json::Map<String, JsonValue>>, DbError> {
    let conn = db.conn.lock().map_err(|e| DbError {
        message: format!("Lock error: {e}"),
    })?;
    let mut stmt = conn
        .prepare("SELECT \"table\", pk, cid, val, col_version, db_version, site_id, cl, seq FROM crsql_changes ORDER BY seq")
        .map_err(|e| DbError { message: format!("Prepare error: {e}") })?;
    let col_count = stmt.column_count();
    let col_names: Vec<String> = (0..col_count)
        .map(|i| stmt.column_name(i).unwrap_or("?").to_string())
        .collect();
    let rows = stmt
        .query_map([], |row| {
            let mut obj = serde_json::Map::with_capacity(col_count);
            for i in 0..col_count {
                obj.insert(col_names[i].clone(), row_to_json(row, i));
            }
            Ok(obj)
        })
        .map_err(|e| DbError {
            message: format!("Query error: {e}"),
        })?;
    let mut result = Vec::new();
    for row in rows {
        result.push(row.map_err(|e| DbError {
            message: format!("Row error: {e}"),
        })?);
    }
    Ok(result)
}

// ─── Helpers ────────────────────────────────────────────────────────────

fn json_to_rusqlite(v: JsonValue) -> Result<rusqlite::types::Value, DbError> {
    match v {
        JsonValue::Null => Ok(rusqlite::types::Value::Null),
        JsonValue::Bool(b) => Ok(rusqlite::types::Value::Integer(if b { 1 } else { 0 })),
        JsonValue::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(rusqlite::types::Value::Integer(i))
            } else if let Some(f) = n.as_f64() {
                Ok(rusqlite::types::Value::Real(f))
            } else {
                // Fallback: try to parse as integer or real from string
                if let Ok(i) = n.to_string().parse::<i64>() {
                    Ok(rusqlite::types::Value::Integer(i))
                } else if let Ok(f) = n.to_string().parse::<f64>() {
                    Ok(rusqlite::types::Value::Real(f))
                } else {
                    Ok(rusqlite::types::Value::Text(n.to_string()))
                }
            }
        }
        JsonValue::String(s) => Ok(rusqlite::types::Value::Text(s)),
        JsonValue::Array(arr) => Ok(rusqlite::types::Value::Text(
            serde_json::to_string(&arr).unwrap_or_default(),
        )),
        JsonValue::Object(obj) => {
            if obj.len() == 1 {
                if let Some(JsonValue::String(encoded)) = obj.get("__laso_blob_b64") {
                    let bytes = BASE64_STANDARD.decode(encoded).map_err(|error| DbError {
                        message: format!("Invalid SQLite blob transport value: {error}"),
                    })?;
                    return Ok(rusqlite::types::Value::Blob(bytes));
                }
            }
            Ok(rusqlite::types::Value::Text(
                serde_json::to_string(&obj).unwrap_or_default(),
            ))
        }
    }
}

fn row_to_json(row: &rusqlite::Row, i: usize) -> JsonValue {
    match row.get_ref(i) {
        Ok(rusqlite::types::ValueRef::Null) => JsonValue::Null,
        Ok(rusqlite::types::ValueRef::Integer(n)) => JsonValue::Number(n.into()),
        Ok(rusqlite::types::ValueRef::Real(f)) => {
            if let Some(n) = serde_json::Number::from_f64(f) {
                JsonValue::Number(n)
            } else {
                JsonValue::Null
            }
        }
        Ok(rusqlite::types::ValueRef::Text(s)) => {
            let s = std::str::from_utf8(s).unwrap_or("");
            JsonValue::String(s.to_string())
        }
        Ok(rusqlite::types::ValueRef::Blob(b)) => JsonValue::String(base64_encode(b)),
        Err(_) => JsonValue::Null,
    }
}

fn base64_encode(data: &[u8]) -> String {
    // Simple base64 encoding without external dependency
    const CHARS: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut result = String::from("b64:");
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = chunk.get(1).copied().unwrap_or(0) as u32;
        let b2 = chunk.get(2).copied().unwrap_or(0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        result.push(CHARS[((triple >> 18) & 0x3F) as usize] as char);
        result.push(CHARS[((triple >> 12) & 0x3F) as usize] as char);
        if chunk.len() > 1 {
            result.push(CHARS[((triple >> 6) & 0x3F) as usize] as char);
        } else {
            result.push('=');
        }
        if chunk.len() > 2 {
            result.push(CHARS[(triple & 0x3F) as usize] as char);
        } else {
            result.push('=');
        }
    }
    result
}
