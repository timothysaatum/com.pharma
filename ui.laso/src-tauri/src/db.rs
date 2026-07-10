use rusqlite::{Connection, params_from_iter};
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;
use std::sync::Mutex;
use tauri::State;
use std::path::PathBuf;

pub struct DbState {
    pub conn: Mutex<Connection>,
}

#[derive(Serialize)]
pub struct ExecResult {
    pub rows_affected: usize,
    pub last_insert_id: Option<i64>,
}

#[derive(Serialize)]
pub struct DbError {
    pub message: String,
}

impl From<rusqlite::Error> for DbError {
    fn from(e: rusqlite::Error) -> Self {
        DbError { message: e.to_string() }
    }
}

/// Resolve the cr-sqlite shared-library path at runtime.
///
/// Resolution order (first match wins):
///   1. `CRSQLITE_EXTENSION_PATH` environment variable
///   2. Tauri resource directory (`resource_dir/crsqlite.so`) — for production bundling
///   3. `../crsqlite.so` — relative from `src-tauri`, works with `tauri dev`
///   4. `crsqlite.so` — next to the binary, general fallback
pub fn resolve_extension_path(resource_dir: Option<PathBuf>) -> Result<PathBuf, String> {
    // 1. Environment variable (highest priority)
    if let Ok(val) = std::env::var("CRSQLITE_EXTENSION_PATH") {
        let p = PathBuf::from(&val);
        if p.exists() {
            return Ok(p);
        }
    }

    // Build candidate list
    let mut candidates: Vec<PathBuf> = Vec::new();

    // 2. Tauri resource directory (used in production bundles)
    if let Some(dir) = &resource_dir {
        candidates.push(dir.join("crsqlite.so"));
    }

    // 3. Relative from src-tauri (works with `tauri dev`)
    candidates.push(PathBuf::from("../crsqlite.so"));

    // 4. Current working directory fallback
    candidates.push(PathBuf::from("crsqlite.so"));

    for p in &candidates {
        if p.exists() {
            return Ok(p.clone());
        }
    }

    Err(format!(
        "cr-sqlite extension not found. Tried: env CRSQLITE_EXTENSION_PATH, {candidates:?}"
    ))
}

pub fn init_db(
    db_dir: Option<PathBuf>,
    ext_path: Option<PathBuf>,
) -> Result<DbState, String> {
    // Resolve DB path: use app's data dir or fallback to CWD
    let db_dir = db_dir.unwrap_or_else(|| PathBuf::from("."));
    std::fs::create_dir_all(&db_dir).map_err(|e| format!("Cannot create DB dir: {e}"))?;

    let db_path = db_dir.join("laso.db");
    let mut conn = Connection::open(&db_path)
        .map_err(|e| format!("Cannot open SQLite DB: {e}"))?;

    // Enable WAL mode
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")
        .map_err(|e| format!("Cannot set pragmas: {e}"))?;

    // Load cr-sqlite extension (if a path was resolved)
    if let Some(ext_path) = ext_path {
        // SAFETY: cr-sqlite is a trusted extension shipped with the app
        unsafe {
            conn.load_extension(ext_path, None)
                .map_err(|e| format!("Cannot load cr-sqlite extension: {e}"))?;
        }
        // Verify extension loaded
        let site_id: String = conn
            .query_row("SELECT crsql_site_id()", [], |row| row.get(0))
            .map_err(|e| format!("cr-sqlite loaded but crsql_site_id() failed: {e}"))?;
        println!("[db] cr-sqlite loaded, site_id={site_id}");
    } else {
        println!("[db] cr-sqlite extension not found, running without CRDT support");
    }

    Ok(DbState {
        conn: Mutex::new(conn),
    })
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
        .collect();

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
        .collect();

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
    conn.execute_batch(&sql)
        .map_err(|e| DbError {
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
        last_insert_id: if last_insert_id == 0 { None } else { Some(last_insert_id) },
    })
}

/// Check crsql_changes table for entries (for testing)
#[tauri::command]
pub fn db_get_crsql_changes(db: State<DbState>) -> Result<Vec<serde_json::Map<String, JsonValue>>, DbError> {
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
        .map_err(|e| DbError { message: format!("Query error: {e}") })?;
    let mut result = Vec::new();
    for row in rows {
        result.push(row.map_err(|e| DbError { message: format!("Row error: {e}") })?);
    }
    Ok(result)
}

// ─── Helpers ────────────────────────────────────────────────────────────

fn json_to_rusqlite(v: JsonValue) -> rusqlite::types::Value {
    match v {
        JsonValue::Null => rusqlite::types::Value::Null,
        JsonValue::Bool(b) => rusqlite::types::Value::Integer(if b { 1 } else { 0 }),
        JsonValue::Number(n) => {
            if let Some(i) = n.as_i64() {
                rusqlite::types::Value::Integer(i)
            } else if let Some(f) = n.as_f64() {
                rusqlite::types::Value::Real(f)
            } else {
                // Fallback: try to parse as integer or real from string
                if let Ok(i) = n.to_string().parse::<i64>() {
                    rusqlite::types::Value::Integer(i)
                } else if let Ok(f) = n.to_string().parse::<f64>() {
                    rusqlite::types::Value::Real(f)
                } else {
                    rusqlite::types::Value::Text(n.to_string())
                }
            }
        }
        JsonValue::String(s) => rusqlite::types::Value::Text(s),
        JsonValue::Array(arr) => {
            rusqlite::types::Value::Text(serde_json::to_string(&arr).unwrap_or_default())
        }
        JsonValue::Object(obj) => {
            rusqlite::types::Value::Text(serde_json::to_string(&obj).unwrap_or_default())
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
        Ok(rusqlite::types::ValueRef::Blob(b)) => {
            JsonValue::String(base64_encode(b))
        }
        Err(_) => JsonValue::Null,
    }
}

fn base64_encode(data: &[u8]) -> String {
    // Simple base64 encoding without external dependency
    const CHARS: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut result = String::new();
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
