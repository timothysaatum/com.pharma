use base64::{engine::general_purpose::STANDARD as BASE64_STANDARD, Engine as _};
use rand::{rngs::OsRng, RngCore};
use rusqlite::{params_from_iter, Connection, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;
use std::io::Read;
use std::path::{Path, PathBuf};
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

// ─── SQLCipher at-rest encryption ──────────────────────────────────────
//
// `laso.db` holds prescriptions, customer PII, and controlled-substance
// dispensing records, so it must never sit on disk unencrypted. This
// section generates/manages the SQLCipher raw key format and drives a
// crash-safe, resumable migration from a plaintext install to an
// encrypted one. The safety boundary is atomic filesystem rename (SQLite
// has no cross-file transaction primitive), matching the bar of care set
// by this codebase's `migrate_v15`/`repairIncompleteV15Migration` pattern
// in `ui.laso/src/lib/localDb.ts`: detect a specific stuck state on every
// launch, heal it, no-op otherwise — never trust a one-shot flag.

const PLAINTEXT_SQLITE_MAGIC: &[u8; 16] = b"SQLite format 3\0";

/// Generate a fresh SQLCipher raw-key (32 CSPRNG bytes, hex-encoded as
/// `x'<64 hex chars>'`). Never derived from anything user-typed.
pub(crate) fn generate_sqlcipher_raw_key() -> String {
    let mut bytes = [0u8; 32];
    OsRng.fill_bytes(&mut bytes);
    let hex: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    format!("x'{hex}'")
}

/// Reads the first 16 bytes of a file, if it exists and is long enough.
fn read_header_bytes(path: &Path) -> Option<[u8; 16]> {
    let mut file = std::fs::File::open(path).ok()?;
    let mut buf = [0u8; 16];
    file.read_exact(&mut buf).ok()?;
    Some(buf)
}

/// Cheap plaintext-vs-encrypted detection: plaintext SQLite files start
/// with the literal magic header `"SQLite format 3\0"`; SQLCipher output
/// does not (it's the ciphertext of the first page). A missing/empty/short
/// file is treated as "not plaintext" (fresh install path).
fn is_plaintext_sqlite(path: &Path) -> bool {
    read_header_bytes(path).as_ref() == Some(PLAINTEXT_SQLITE_MAGIC)
}

fn with_suffix(path: &Path, suffix: &str) -> PathBuf {
    let mut s = path.as_os_str().to_owned();
    s.push(suffix);
    PathBuf::from(s)
}

fn migration_tmp_path(db_path: &Path) -> PathBuf {
    with_suffix(db_path, ".encrypting-tmp")
}

fn migration_bak_path(db_path: &Path) -> PathBuf {
    with_suffix(db_path, ".pre-encryption.bak")
}

/// WAL/SHM sidecar files SQLite maintains next to a database file. Journal
/// mode is a persistent property of the file itself, so a plaintext DB that
/// was ever run in WAL mode (which this app always enables) can have recent
/// writes sitting in `<db>-wal` rather than `<db>` — and leaving a stale
/// plaintext `-wal` file lying around after migration would be exactly the
/// kind of residual-PII-on-disk leak this change exists to close.
fn sidecar_paths(db_path: &Path) -> (PathBuf, PathBuf) {
    (with_suffix(db_path, "-wal"), with_suffix(db_path, "-shm"))
}

fn remove_with_sidecars(path: &Path) {
    let _ = std::fs::remove_file(path);
    let (wal, shm) = sidecar_paths(path);
    let _ = std::fs::remove_file(&wal);
    let _ = std::fs::remove_file(&shm);
}

/// Rename `from` to `to`, carrying along any WAL/SHM sidecars so they don't
/// linger under the old filename (or collide with the destination's future
/// sidecars).
fn rename_with_sidecars(from: &Path, to: &Path) -> std::io::Result<()> {
    std::fs::rename(from, to)?;
    let (from_wal, from_shm) = sidecar_paths(from);
    let (to_wal, to_shm) = sidecar_paths(to);
    if from_wal.exists() {
        let _ = std::fs::rename(&from_wal, &to_wal);
    }
    if from_shm.exists() {
        let _ = std::fs::rename(&from_shm, &to_shm);
    }
    Ok(())
}

fn sql_string_literal(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('\'');
    for ch in s.chars() {
        if ch == '\'' {
            out.push('\'');
        }
        out.push(ch);
    }
    out.push('\'');
    out
}

fn open_and_key(path: &Path, key: &str) -> Result<Connection, String> {
    let conn = Connection::open(path)
        .map_err(|e| format!("Cannot open {}: {e}", path.display()))?;
    conn.pragma_update(None, "key", key)
        .map_err(|e| format!("Cannot set SQLCipher key on {}: {e}", path.display()))?;
    Ok(conn)
}

/// Integrity-only check on an already-encrypted database: confirms every
/// page decrypts (`cipher_integrity_check`) and the resulting SQLite
/// structure is sound (`integrity_check`). Deliberately does NOT compare
/// row counts against anything — this is also used to validate a
/// live, already-migrated DB that may have accumulated new data since
/// migration, where row counts are expected to have grown.
fn check_encrypted_integrity(conn: &Connection) -> Result<(), String> {
    let mut cipher_issues: Vec<String> = Vec::new();
    {
        let mut stmt = conn
            .prepare("PRAGMA cipher_integrity_check")
            .map_err(|e| format!("cipher_integrity_check failed: {e}"))?;
        let rows = stmt
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(|e| format!("cipher_integrity_check failed: {e}"))?;
        for row in rows {
            cipher_issues.push(row.map_err(|e| format!("cipher_integrity_check row error: {e}"))?);
        }
    }
    if !cipher_issues.is_empty() {
        return Err(format!(
            "cipher_integrity_check reported issues: {}",
            cipher_issues.join("; ")
        ));
    }

    let integrity: String = conn
        .query_row("PRAGMA integrity_check", [], |row| row.get(0))
        .map_err(|e| format!("integrity_check failed: {e}"))?;
    if integrity.to_lowercase() != "ok" {
        return Err(format!("integrity_check reported: {integrity}"));
    }
    Ok(())
}

fn verify_encrypted_db_intact(db_path: &Path, key: &str) -> Result<(), String> {
    let conn = open_and_key(db_path, key)?;
    check_encrypted_integrity(&conn)
}

/// Full pre-finalize verification of a freshly-built encrypted copy: the
/// integrity checks above, PLUS an exact row-count comparison against the
/// still-untouched plaintext original that produced it. Row-count equality
/// only makes sense here because, at this point, nothing has written to the
/// new copy since it was exported — it's not yet "live".
fn verify_migrated_db(plain_path: &Path, encrypted_path: &Path, key: &str) -> Result<(), String> {
    let encrypted_conn = open_and_key(encrypted_path, key)?;
    check_encrypted_integrity(&encrypted_conn)?;

    let plain_conn = Connection::open(plain_path)
        .map_err(|e| format!("Cannot reopen plaintext original for verification: {e}"))?;

    let table_names: Vec<String> = {
        let mut stmt = plain_conn
            .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            .map_err(|e| format!("Cannot list tables: {e}"))?;
        let rows = stmt
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(|e| format!("Cannot list tables: {e}"))?;
        rows.collect::<Result<_, _>>()
            .map_err(|e| format!("Cannot list tables: {e}"))?
    };

    for table in table_names {
        let quoted = format!("\"{}\"", table.replace('"', "\"\""));
        let plain_count: i64 = plain_conn
            .query_row(&format!("SELECT COUNT(*) FROM {quoted}"), [], |row| row.get(0))
            .map_err(|e| format!("Cannot count rows in {table} (plaintext): {e}"))?;
        let migrated_count: i64 = encrypted_conn
            .query_row(&format!("SELECT COUNT(*) FROM {quoted}"), [], |row| row.get(0))
            .map_err(|e| format!("Cannot count rows in {table} (migrated): {e}"))?;
        if plain_count != migrated_count {
            return Err(format!(
                "Row count mismatch in table {table}: plaintext={plain_count}, migrated={migrated_count}"
            ));
        }
    }

    Ok(())
}

/// Build a new encrypted copy of `plain_path` at `tmp_path` via SQLCipher's
/// standard plaintext-to-encrypted conversion. Never touches `plain_path`.
fn build_encrypted_copy(plain_path: &Path, tmp_path: &Path, key: &str) -> Result<(), String> {
    // Clean up any stray leftover from a previous crashed attempt so we
    // always start from a clean slate.
    remove_with_sidecars(tmp_path);

    let conn = Connection::open(plain_path)
        .map_err(|e| format!("Cannot open plaintext DB for migration: {e}"))?;

    let attach_sql = format!(
        "ATTACH DATABASE {} AS encrypted KEY {};",
        sql_string_literal(&tmp_path.to_string_lossy()),
        sql_string_literal(key),
    );
    conn.execute_batch(&attach_sql)
        .map_err(|e| format!("Cannot attach encrypted target DB: {e}"))?;

    conn.query_row("SELECT sqlcipher_export('encrypted')", [], |_row| Ok(()))
        .map_err(|e| format!("sqlcipher_export failed: {e}"))?;

    conn.execute_batch("DETACH DATABASE encrypted;")
        .map_err(|e| format!("Cannot detach encrypted target DB: {e}"))?;

    Ok(())
}

/// Drives the crash-safe, resumable plaintext -> encrypted migration.
///
/// Returns `Ok(Some(warning))` when the app should still start (falling
/// back to whatever DB state is safely usable) but something worth
/// surfacing happened; `Ok(None)` when everything is normal; `Err` only
/// when this function cannot guarantee `db_path` is left in a directly
/// openable state (an unrecoverable filesystem failure) — the caller
/// treats that as fatal, same as this codebase already treats a missing
/// cr-sqlite extension as fatal, rather than silently risking data loss.
///
/// Invariant on every non-`Err` return: `db_path` either does not exist
/// (fresh install — the caller will create it) or exists and is directly
/// openable, and `is_plaintext_sqlite(db_path)` correctly reflects whether
/// the caller should set `PRAGMA key` on it.
fn ensure_db_ready(db_path: &Path, key: &str) -> Result<Option<String>, String> {
    let bak_path = migration_bak_path(db_path);
    let tmp_path = migration_tmp_path(db_path);

    // ── Heal the one genuinely stateful crash window: a kill between the
    // two renames in the "finalize" step below (original safely renamed to
    // `.bak`, but the new encrypted copy never made it into place as
    // `laso.db`). ──
    if !db_path.exists() && bak_path.exists() {
        if tmp_path.exists() && verify_migrated_db(&bak_path, &tmp_path, key).is_ok() {
            std::fs::rename(&tmp_path, db_path).map_err(|e| {
                format!(
                    "Self-heal: verified encrypted copy exists but could not be placed at {}: {e}. \
                     Original plaintext DB remains safe at {}.",
                    db_path.display(),
                    bak_path.display()
                )
            })?;
            // Migration just completed via self-heal. Keep `.bak` for one
            // more successful launch before deleting it (checked below on
            // a later call), same as the non-crash path.
            return Ok(Some(
                "DB encryption migration self-healed after an interrupted previous launch".to_string(),
            ));
        }

        // The new copy is missing or failed verification: restore the
        // known-good plaintext backup so the app has a working DB, and
        // fall through to retry migration from scratch below.
        remove_with_sidecars(&tmp_path);
        rename_with_sidecars(&bak_path, db_path).map_err(|e| {
            format!(
                "Self-heal: could not restore pre-encryption backup {} to {}: {e}. \
                 Manual recovery required.",
                bak_path.display(),
                db_path.display()
            )
        })?;
    }

    if !db_path.exists() {
        // Fresh install: nothing to migrate. Clear any stray tmp file just
        // in case (harmless if none exists).
        remove_with_sidecars(&tmp_path);
        return Ok(None);
    }

    if is_plaintext_sqlite(db_path) {
        if let Err(e) = build_encrypted_copy(db_path, &tmp_path, key) {
            remove_with_sidecars(&tmp_path);
            return Ok(Some(format!(
                "DB encryption migration failed while building encrypted copy, original untouched, will retry next launch: {e}"
            )));
        }
        if let Err(e) = verify_migrated_db(db_path, &tmp_path, key) {
            remove_with_sidecars(&tmp_path);
            return Ok(Some(format!(
                "DB encryption migration failed verification, original untouched, will retry next launch: {e}"
            )));
        }

        // Verified good. Finalize: original -> backup, then new copy -> live.
        // This pair of renames is the one window a crash can land in — see
        // the heal branch above for how it's detected and repaired.
        rename_with_sidecars(db_path, &bak_path)
            .map_err(|e| format!("DB encryption migration could not back up original: {e}"))?;

        if let Err(rename_err) = std::fs::rename(&tmp_path, db_path) {
            // Try once more (transient FS hiccups), else restore the
            // backup so this launch still has a working DB rather than
            // leaving db_path missing (which `Connection::open` would
            // otherwise silently "fix" by creating an empty database).
            if tmp_path.exists() && std::fs::rename(&tmp_path, db_path).is_ok() {
                return Ok(Some(format!(
                    "DB encryption migration finalize rename failed once but succeeded on retry: {rename_err}"
                )));
            }
            rename_with_sidecars(&bak_path, db_path).map_err(|restore_err| {
                format!(
                    "DB encryption migration finalize failed ({rename_err}) and restoring the plaintext \
                     backup also failed ({restore_err}). Manual recovery required using {} and {}.",
                    bak_path.display(),
                    tmp_path.display()
                )
            })?;
            return Ok(Some(format!(
                "DB encryption migration finalize failed, restored plaintext backup, will retry next launch: {rename_err}"
            )));
        }

        return Ok(Some(
            "DB successfully migrated to encrypted storage".to_string(),
        ));
    }

    // `db_path` exists and is not plaintext: already encrypted, either from
    // a completed migration or a fresh SQLCipher install from a prior run.
    // Grace-period cleanup: only delete the backup once this DB opens and
    // passes an integrity check on a LATER launch than the one that
    // created it (never trust a one-shot completion flag).
    if bak_path.exists() {
        if let Err(e) = verify_encrypted_db_intact(db_path, key) {
            return Ok(Some(format!(
                "Post-migration DB failed integrity verification; keeping backup at {} until this is resolved: {e}",
                bak_path.display()
            )));
        }
        remove_with_sidecars(&bak_path);
    }

    Ok(None)
}

pub fn init_db(
    db_dir: Option<PathBuf>,
    encryption_key: &str,
) -> Result<DbState, String> {
    // Resolve DB path: use app's data dir or fallback to CWD
    let db_dir = db_dir.unwrap_or_else(|| PathBuf::from("."));
    std::fs::create_dir_all(&db_dir).map_err(|e| format!("Cannot create DB dir: {e}"))?;

    let db_path = db_dir.join("laso.db");

    let migration_warning = ensure_db_ready(&db_path, encryption_key)?;

    // Whether `PRAGMA key` should be set on this connection: it must be set
    // for a fresh install (so the file is created encrypted) and for an
    // already-encrypted file, but must NOT be set when `ensure_db_ready`
    // had to fall back to an intact plaintext DB (SQLCipher only decodes
    // as ciphertext once a key is set — setting one on a genuinely
    // plaintext file would make it unreadable).
    let needs_key = !is_plaintext_sqlite(&db_path);

    let conn = Connection::open(&db_path).map_err(|e| format!("Cannot open SQLite DB: {e}"))?;

    // PRAGMA key MUST be the very first statement executed on this
    // connection — SQLCipher requires it before any other pragma or query.
    if needs_key {
        conn.pragma_update(None, "key", encryption_key)
            .map_err(|e| format!("Cannot set SQLCipher key: {e}"))?;
    }

    // Enable WAL mode
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")
        .map_err(|e| format!("Cannot set pragmas: {e}"))?;

    Ok(DbState {
        conn: Mutex::new(conn),
        startup_warning: migration_warning,
    })
}

#[cfg(test)]
mod startup_tests {
    use super::{
        build_encrypted_copy, ensure_db_ready, execute_transaction,
        generate_sqlcipher_raw_key, init_db, is_plaintext_sqlite,
        json_to_rusqlite, migration_bak_path, migration_tmp_path, open_and_key,
        rename_with_sidecars, TransactionStatement,
        PLAINTEXT_SQLITE_MAGIC,
    };
    use rusqlite::{types::Value, Connection};

    /// Fixed key for tests — production always uses
    /// `generate_sqlcipher_raw_key()` via the OS keyring; tests inject an
    /// explicit key so they never touch the real keyring (see
    /// `get_or_create_key_from_store` in `lib.rs` for that round-trip test).
    const TEST_KEY: &str =
        "x'0202020202020202020202020202020202020202020202020202020202020202'";

    fn fresh_temp_dir(label: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "pharmacare-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn generate_sqlcipher_raw_key_is_well_formed_and_random() {
        let a = generate_sqlcipher_raw_key();
        let b = generate_sqlcipher_raw_key();
        assert_ne!(a, b, "two generated keys should not collide");
        for key in [&a, &b] {
            assert!(
                key.starts_with("x'") && key.ends_with('\''),
                "key should use SQLCipher raw-key format: {key}"
            );
            let hex = &key[2..key.len() - 1];
            assert_eq!(hex.len(), 64, "should encode 32 bytes as 64 hex chars: {key}");
            assert!(
                hex.chars().all(|c| c.is_ascii_hexdigit()),
                "should be all hex digits: {key}"
            );
        }
    }

    #[test]
    fn fresh_install_creates_encrypted_db() {
        let temp = fresh_temp_dir("fresh-install-encrypted");

        let state = init_db(Some(temp.clone()), TEST_KEY)
            .expect("fresh install should initialize");
        assert_eq!(state.startup_warning, None);
        drop(state);

        let db_path = temp.join("laso.db");
        let mut header = [0u8; 16];
        {
            use std::io::Read;
            let mut f = std::fs::File::open(&db_path).unwrap();
            f.read_exact(&mut header).unwrap();
        }
        assert_ne!(
            &header, PLAINTEXT_SQLITE_MAGIC,
            "a freshly created laso.db must not carry the plaintext SQLite magic header"
        );

        let _ = std::fs::remove_dir_all(&temp);
    }

    #[test]
    fn ensure_db_ready_migrates_plaintext_without_needing_cr_sqlite() {
        // Exercises the migration state machine directly, decoupled from
        // cr-sqlite extension loading (which happens later in `init_db`).
        let temp = fresh_temp_dir("ensure-db-ready-direct");
        let db_path = temp.join("laso.db");
        {
            let seed = Connection::open(&db_path).unwrap();
            seed.execute_batch(
                "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT); \
                 INSERT INTO customers (id, name) VALUES (1, 'Jane Doe');",
            )
            .unwrap();
        }
        assert!(is_plaintext_sqlite(&db_path));

        let warning = ensure_db_ready(&db_path, TEST_KEY).expect("migration should succeed");
        assert!(
            warning.unwrap_or_default().contains("migrated"),
            "should report a successful migration"
        );
        assert!(!is_plaintext_sqlite(&db_path));
        assert!(
            migration_bak_path(&db_path).exists(),
            "pre-encryption backup should be retained for the grace period"
        );

        let _ = std::fs::remove_dir_all(&temp);
    }

    #[test]
    fn plaintext_db_migrates_to_encrypted_in_place() {
        let temp = fresh_temp_dir("plaintext-migrates");
        let db_path = temp.join("laso.db");

        // Pre-seed a real plaintext SQLite DB with sample rows, as an
        // existing install would have on disk before this feature ships.
        {
            let seed = Connection::open(&db_path).unwrap();
            seed.execute_batch(
                "PRAGMA journal_mode=WAL; \
                 CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT); \
                 INSERT INTO customers (id, name) VALUES (1, 'Jane Doe'), (2, 'John Smith');",
            )
            .unwrap();
        }
        assert!(
            is_plaintext_sqlite(&db_path),
            "seed DB must be plaintext SQLite before migration"
        );

        let state = init_db(Some(temp.clone()), TEST_KEY)
            .expect("plaintext DB should migrate and initialize");

        assert!(
            !is_plaintext_sqlite(&db_path),
            "laso.db should be encrypted after migration"
        );

        {
            let conn = state.conn.lock().unwrap();
            let count: i64 = conn
                .query_row("SELECT COUNT(*) FROM customers", [], |row| row.get(0))
                .unwrap();
            assert_eq!(count, 2, "row count must match the plaintext original exactly");
            let name: String = conn
                .query_row("SELECT name FROM customers WHERE id = 1", [], |row| row.get(0))
                .unwrap();
            assert_eq!(name, "Jane Doe");
        }
        drop(state);

        // Independently reopen with the stored key to prove the data lives
        // in the encrypted file on disk, not just in the live connection.
        let reopened = Connection::open(&db_path).unwrap();
        reopened.pragma_update(None, "key", TEST_KEY).unwrap();
        let count: i64 = reopened
            .query_row("SELECT COUNT(*) FROM customers", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 2);

        assert!(
            migration_bak_path(&db_path).exists(),
            "pre-encryption backup should be retained for one more launch, not deleted immediately"
        );

        let _ = std::fs::remove_dir_all(&temp);
    }

    #[test]
    fn killed_mid_migration_self_heals_on_next_launch() {
        let temp = fresh_temp_dir("killed-mid-migration");
        let db_path = temp.join("laso.db");

        {
            let seed = Connection::open(&db_path).unwrap();
            seed.execute_batch(
                "PRAGMA journal_mode=WAL; \
                 CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT); \
                 INSERT INTO customers (id, name) VALUES (1, 'Jane Doe'), (2, 'John Smith');",
            )
            .unwrap();
        }

        let tmp_path = migration_tmp_path(&db_path);
        let bak_path = migration_bak_path(&db_path);

        // Simulate a kill in the middle of the finalize step: the fully
        // verified encrypted copy has been built, and the original has
        // just been renamed to `.bak` — but the second rename (tmp ->
        // live) never happened.
        build_encrypted_copy(&db_path, &tmp_path, TEST_KEY).expect("build encrypted copy");
        rename_with_sidecars(&db_path, &bak_path).expect("rename original to backup");
        assert!(!db_path.exists(), "simulated crash window: laso.db should be missing");
        assert!(bak_path.exists());
        assert!(tmp_path.exists());

        // Next launch: init_db must detect and repair this without any
        // data loss, in a single call.
        let state = init_db(Some(temp.clone()), TEST_KEY)
            .expect("self-heal should succeed and initialize normally");

        assert!(db_path.exists(), "laso.db should exist again after self-heal");
        assert!(!is_plaintext_sqlite(&db_path), "healed laso.db should be encrypted");
        assert!(
            state
                .startup_warning
                .as_deref()
                .unwrap_or("")
                .contains("self-heal"),
            "startup_warning should mention the self-heal: {:?}",
            state.startup_warning
        );

        {
            let conn = state.conn.lock().unwrap();
            let count: i64 = conn
                .query_row("SELECT COUNT(*) FROM customers", [], |row| row.get(0))
                .unwrap();
            assert_eq!(count, 2, "no data loss across the self-heal");
            let name: String = conn
                .query_row("SELECT name FROM customers WHERE id = 1", [], |row| row.get(0))
                .unwrap();
            assert_eq!(name, "Jane Doe");
        }

        // The backup is intentionally kept for one more successful launch
        // before being cleaned up — it should still be here right after
        // the heal itself.
        assert!(
            bak_path.exists(),
            "backup should be retained through the heal launch itself"
        );
        drop(state);

        // A follow-up successful launch on the now-healed, now-encrypted DB
        // is what finally cleans up the backup.
        let state2 = init_db(
            Some(temp.clone()),
            TEST_KEY,
        )
        .expect("follow-up launch should succeed");
        assert!(
            !bak_path.exists(),
            "backup should be cleaned up after a later successful launch"
        );
        drop(state2);

        let _ = std::fs::remove_dir_all(&temp);
    }

    /// Not a real test on its own — spawned as a genuine OS subprocess by
    /// `real_process_kill_during_encrypted_copy_leaves_state_ensure_db_ready_can_repair`
    /// so that test can send it a real SIGKILL mid-copy, rather than only
    /// hand-constructing the crash-window file state (as the test above
    /// does). `#[ignore]` keeps a normal `cargo test` run from executing it
    /// directly; it only runs via the explicit `--exact --ignored` spawn.
    #[test]
    #[ignore]
    fn _migration_worker_build_encrypted_copy() {
        let db_path = std::path::PathBuf::from(
            std::env::var("MIGRATION_WORKER_DB_PATH").expect("MIGRATION_WORKER_DB_PATH not set"),
        );
        let marker_path = std::path::PathBuf::from(
            std::env::var("MIGRATION_WORKER_MARKER_PATH")
                .expect("MIGRATION_WORKER_MARKER_PATH not set"),
        );
        let tmp_path = migration_tmp_path(&db_path);
        // Signal "about to start the copy" before doing any real work, so
        // the parent knows exactly when it's safe to kill us.
        std::fs::write(&marker_path, b"started").unwrap();
        let _ = build_encrypted_copy(&db_path, &tmp_path, TEST_KEY);
    }

    #[test]
    fn real_process_kill_during_encrypted_copy_leaves_state_ensure_db_ready_can_repair() {
        use std::process::{Command, Stdio};
        use std::time::{Duration, Instant};

        let temp = fresh_temp_dir("real-kill-mid-copy");
        let db_path = temp.join("laso.db");

        // Seed enough rows that sqlcipher_export takes long enough to
        // reliably interrupt with a real signal, not just a hand-built
        // intermediate file state.
        {
            let seed = Connection::open(&db_path).unwrap();
            seed.execute_batch(
                "PRAGMA journal_mode=WAL; \
                 CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);",
            )
            .unwrap();
            seed.execute_batch("BEGIN;").unwrap();
            {
                let mut stmt = seed
                    .prepare("INSERT INTO customers (id, name) VALUES (?1, ?2)")
                    .unwrap();
                for i in 0..300_000i64 {
                    stmt.execute(rusqlite::params![i, format!("Patient {i}")])
                        .unwrap();
                }
            }
            seed.execute_batch("COMMIT;").unwrap();
        }
        assert!(is_plaintext_sqlite(&db_path));

        let marker_path = temp.join("worker-started.marker");
        let test_exe = std::env::current_exe().expect("current_exe");
        let mut child = Command::new(&test_exe)
            .arg("db::startup_tests::_migration_worker_build_encrypted_copy")
            .arg("--exact")
            .arg("--ignored")
            .arg("--test-threads=1")
            .env("MIGRATION_WORKER_DB_PATH", &db_path)
            .env("MIGRATION_WORKER_MARKER_PATH", &marker_path)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn migration worker subprocess");

        let deadline = Instant::now() + Duration::from_secs(15);
        while !marker_path.exists() {
            assert!(Instant::now() < deadline, "worker never signaled it started");
            std::thread::sleep(Duration::from_millis(5));
        }
        // Let the worker get genuinely into the export before killing it.
        std::thread::sleep(Duration::from_millis(40));

        let pid = child.id();
        let status = Command::new("kill")
            .arg("-9")
            .arg(pid.to_string())
            .status()
            .expect("send SIGKILL");
        assert!(status.success(), "kill -9 must succeed against the worker pid");
        let _ = child.wait();

        // build_encrypted_copy never writes to plain_path — regardless of
        // exactly when the kill landed, the original must be untouched.
        assert!(
            is_plaintext_sqlite(&db_path),
            "a killed copy must never have touched the original plaintext DB"
        );

        // Next launch: ensure_db_ready must produce a directly openable,
        // correctly encrypted DB with no data loss, from a real interrupted
        // process, not a simulated state.
        let warning = ensure_db_ready(&db_path, TEST_KEY).expect("recovery must not error");
        assert!(!is_plaintext_sqlite(&db_path), "should now be encrypted");
        let _ = warning;

        let conn = open_and_key(&db_path, TEST_KEY).unwrap();
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM customers", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 300_000, "no data loss after a real process kill mid-copy + recovery");
        drop(conn);

        let _ = std::fs::remove_dir_all(&temp);
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
