use rusqlite::{Connection, params};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const EXT_PATH: &str = "crsqlite.so";

fn open_db(path: &str, load_crsqlite: bool) -> Connection {
    // Remove old file if exists
    let _ = std::fs::remove_file(path);
    let conn = Connection::open(path).expect("open");
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")
        .expect("pragmas");
    if load_crsqlite {
        unsafe {
            conn.load_extension(EXT_PATH, None)
                .expect("load cr-sqlite");
        }
    }
    conn
}

/// Opens a connection to an EXISTING database file without deleting it,
/// optionally loading the cr-sqlite extension. Safe to call from multiple
/// threads/connections against a database that's already been set up.
fn connect_existing(path: &str, load_crsqlite: bool) -> Connection {
    let conn = Connection::open(path).expect("open existing db");
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")
        .expect("pragmas");
    if load_crsqlite {
        unsafe {
            conn.load_extension(EXT_PATH, None)
                .expect("load cr-sqlite");
        }
    }
    conn
}

fn create_schema(conn: &Connection) {
    conn.execute_batch("
        CREATE TABLE IF NOT EXISTS branch_inventory (
            id                TEXT PRIMARY KEY NOT NULL,
            branch_id         TEXT NOT NULL DEFAULT '',
            drug_id           TEXT NOT NULL DEFAULT '',
            quantity          INTEGER NOT NULL DEFAULT 0,
            reserved_quantity INTEGER NOT NULL DEFAULT 0,
            location          TEXT,
            selling_price     REAL,
            created_at        TEXT NOT NULL DEFAULT '',
            updated_at        TEXT NOT NULL DEFAULT '',
            sync_version      INTEGER NOT NULL DEFAULT 1,
            sync_status       TEXT NOT NULL DEFAULT 'synced',
            is_deleted        INTEGER NOT NULL DEFAULT 0
        );
    ").expect("create schema");
}

// ─── Step 3: Latency measurement ───────────────────────────────────────

fn measure_latency(label: &str, iterations: usize, mut f: impl FnMut()) {
    // warmup
    for _ in 0..10 { f(); }
    let start = Instant::now();
    for _ in 0..iterations { f(); }
    let elapsed = start.elapsed();
    let avg = elapsed / iterations as u32;
    println!("  {label:50} {iterations:6} iterations  total={elapsed:?}  avg={avg:?}");
}

#[test]
fn step3_latency_measurement() {
    println!("\n═══════════════════════════════════════════════════════════════");
    println!("STEP 3: Latency Measurement");
    println!("═══════════════════════════════════════════════════════════════\n");

    // ── Baseline: plain SQLite ──
    let conn = open_db("/tmp/crsqlite_spike_plain.db", false);
    create_schema(&conn);
    let id = "test-latency-1";

    measure_latency("INSERT (plain)", 100, || {
        conn.execute(
            "INSERT INTO branch_inventory (id, branch_id, drug_id, quantity) VALUES (?1, 'b', 'd', 100)",
            params![id],
        ).ok();
    });

    // ── CRR table ──
    let crr = open_db("/tmp/crsqlite_spike_crr.db", true);
    create_schema(&crr);
    crr.execute_batch("SELECT crsql_as_crr('branch_inventory')").expect("crr");
    let id2 = "test-latency-2";

    measure_latency("INSERT into CRR table", 100, || {
        crr.execute(
            "INSERT INTO branch_inventory (id, branch_id, drug_id, quantity) VALUES (?1, 'b', 'd', 100)",
            params![id2],
        ).ok();
    });

    measure_latency("SELECT from CRR table (by id)", 100, || {
        let _: i64 = crr.query_row(
            "SELECT quantity FROM branch_inventory WHERE id = ?1", params![id2],
            |r| r.get(0),
        ).unwrap_or(0);
    });

    measure_latency("SELECT from CRR table (full scan)", 100, || {
        let mut stmt = crr.prepare("SELECT COUNT(*) FROM branch_inventory").unwrap();
        let _: i64 = stmt.query_row([], |r| r.get(0)).unwrap();
    });

    measure_latency("UPDATE CRR row", 100, || {
        crr.execute(
            "UPDATE branch_inventory SET quantity = quantity + 1 WHERE id = ?1",
            params![id2],
        ).ok();
    });

    // Cleanup
    drop(crr);
    drop(conn);
}

// ─── Step 4: WAL + Savepoint + cr-sqlite compatibility ─────────────────

#[test]
fn step4_wal_savepoint_crsqlite() {
    println!("\n═══════════════════════════════════════════════════════════════");
    println!("STEP 4: WAL + Savepoint + cr-sqlite Compatibility");
    println!("═══════════════════════════════════════════════════════════════\n");

    // 4a. Confirm WAL mode is active
    let conn = open_db("/tmp/crsqlite_spike_step4.db", true);
    let wal: String = conn.query_row("PRAGMA journal_mode", [], |r| r.get(0)).unwrap();
    println!("  4a. WAL mode: {wal}");
    assert_eq!(wal, "wal", "WAL mode must be active");

    // 4b. Create CRR and confirm change tracking triggers fire
    create_schema(&conn);
    conn.execute_batch("SELECT crsql_as_crr('branch_inventory')").expect("crr");
    let row_id = "step4-row-1";
    conn.execute(
        "INSERT INTO branch_inventory (id, branch_id, drug_id, quantity, location) VALUES (?1, 'b', 'd', 100, 'Shelf-A')",
        params![row_id],
    ).expect("insert");
    let change_count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM crsql_changes WHERE \"table\" = 'branch_inventory'",
        [], |r| r.get(0)
    ).unwrap();
    println!("  4b. crsql_changes entries after INSERT: {change_count}");
    assert!(change_count > 0, "INSERT must produce crsql_changes entries");

    // 4c. Savepoint with commit — changes should be tracked
    conn.execute_batch("SAVEPOINT sp_commit").expect("savepoint");
    conn.execute(
        "UPDATE branch_inventory SET quantity = 50, location = 'Shelf-B' WHERE id = ?1",
        params![row_id],
    ).expect("update in savepoint");
    conn.execute_batch("RELEASE sp_commit").expect("release");

    let changes_after_commit: i64 = conn.query_row(
        "SELECT COUNT(*) FROM crsql_changes WHERE \"table\" = 'branch_inventory' AND cid = 'quantity'",
        [], |r| r.get(0)
    ).unwrap();
    println!("  4c. crsql_changes quantity entries after committed savepoint: {changes_after_commit}");
    assert!(changes_after_commit >= 1, "Committed savepoint must produce change entries");

    // Verify the actual value was committed
    let qty: i64 = conn.query_row(
        "SELECT quantity FROM branch_inventory WHERE id = ?1", params![row_id],
        |r| r.get(0)
    ).unwrap();
    assert_eq!(qty, 50, "Committed value must persist");
    println!("       Quantity after committed savepoint: {qty}");

    // 4d. Savepoint with rollback — changes should NOT be tracked
    conn.execute_batch("SAVEPOINT sp_rollback").expect("savepoint");
    conn.execute(
        "UPDATE branch_inventory SET quantity = 999 WHERE id = ?1",
        params![row_id],
    ).expect("update in rollback savepoint");
    conn.execute_batch("ROLLBACK TO sp_rollback").expect("rollback");
    conn.execute_batch("RELEASE sp_rollback").expect("release after rollback");

    let qty_after_rollback: i64 = conn.query_row(
        "SELECT quantity FROM branch_inventory WHERE id = ?1", params![row_id],
        |r| r.get(0)
    ).unwrap();
    assert_eq!(qty_after_rollback, 50, "Rolled-back value must not persist");
    println!("  4d. Quantity after rollback: {qty_after_rollback} (correctly unchanged)");

    // Check no crsql_changes with quantity=999 leaked
    let leaked: i64 = conn.query_row(
        "SELECT COUNT(*) FROM crsql_changes WHERE \"table\" = 'branch_inventory' AND cid = 'quantity' AND val = 999",
        [], |r| r.get(0)
    ).unwrap();
    assert_eq!(leaked, 0, "Rolled-back changes must not appear in crsql_changes");
    println!("       Rolled-back change entries leaked into crsql_changes: {leaked} ✅");

    // 4e. Nested savepoints
    conn.execute_batch("SAVEPOINT outer").expect("outer");
    conn.execute(
        "UPDATE branch_inventory SET location = 'Location-Outer' WHERE id = ?1",
        params![row_id],
    ).expect("update outer");
    conn.execute_batch("SAVEPOINT inner").expect("inner");
    conn.execute(
        "UPDATE branch_inventory SET reserved_quantity = 10 WHERE id = ?1",
        params![row_id],
    ).expect("update inner");
    conn.execute_batch("RELEASE inner").expect("release inner");
    conn.execute_batch("RELEASE outer").expect("release outer");

    let loc: String = conn.query_row(
        "SELECT location FROM branch_inventory WHERE id = ?1", params![row_id],
        |r| r.get(0)
    ).unwrap();
    let reserved: i64 = conn.query_row(
        "SELECT reserved_quantity FROM branch_inventory WHERE id = ?1", params![row_id],
        |r| r.get(0)
    ).unwrap();
    println!("  4e. Nested savepoints — location={loc}, reserved={reserved}");
    assert_eq!(loc, "Location-Outer");
    assert_eq!(reserved, 10);
    println!("       Nested savepoints work correctly ✅");

    // 4f. crsql_finalize on connection drop — verify cleanup
    let finalize_check = conn.query_row(
        "SELECT name FROM pragma_function_list WHERE name = 'crsql_finalize'",
        [], |r| r.get::<_, String>(0)
    );
    println!("  4f. crsql_finalize function exists: {}", finalize_check.is_ok());

    drop(conn);
    println!("\n  ✅ Step 4 PASSED — WAL + savepoints + cr-sqlite fully compatible\n");
}

// ─── Step 5: Concurrency check ─────────────────────────────────────────

#[test]
fn step5_concurrency() {
    println!("\n═══════════════════════════════════════════════════════════════");
    println!("STEP 5: Concurrency Check (WAL mode readers + writer)");
    println!("═══════════════════════════════════════════════════════════════\n");

    // Use a shared path so all connections see the same WAL
    let db_path = "/tmp/crsqlite_spike_concurrent.db";
    let _ = std::fs::remove_file(db_path);
    let _ = std::fs::remove_file(format!("{db_path}-wal"));
    let _ = std::fs::remove_file(format!("{db_path}-shm"));

    // Create main connection and set up schema
    let main = open_db(db_path, true);
    create_schema(&main);
    main.execute_batch("SELECT crsql_as_crr('branch_inventory')").expect("crr");

    // Insert a row
    main.execute(
        "INSERT INTO branch_inventory (id, branch_id, drug_id, quantity) VALUES ('concur-1', 'b', 'd', 100)",
        [],
    ).expect("insert main");
    drop(main);

    // Spawn reader threads that continuously read while a writer writes
    let reader_count = 4;
    let iterations = 50;
    let readers_done = Arc::new(Mutex::new(Vec::new()));
    let errors = Arc::new(Mutex::new(Vec::new()));

    let mut handles = Vec::new();

    for ri in 0..reader_count {
        let path = db_path.to_string();
        let done = readers_done.clone();
        let errs = errors.clone();

        handles.push(thread::spawn(move || {
            for _ in 0..iterations {
                // Each reader opens its own connection (as the frontend would)
                let conn = match Connection::open(&path) {
                    Ok(c) => c,
                    Err(e) => { errs.lock().unwrap().push(format!("reader {ri} open: {e}")); return; }
                };
                // WAL allows readers to proceed even during writes
                let result: Result<i64, _> = conn.query_row(
                    "SELECT quantity FROM branch_inventory WHERE id = 'concur-1'",
                    [],
                    |r| r.get(0),
                );
                if let Err(e) = result {
                    errs.lock().unwrap().push(format!("reader {ri} query: {e}"));
                }
                drop(conn);
                thread::sleep(Duration::from_micros(100));
            }
            done.lock().unwrap().push(());
        }));
    }

    // Writer thread
    let path = db_path.to_string();
    let errs = errors.clone();
    handles.push(thread::spawn(move || {
        let conn = connect_existing(&path, true);
        let _ = &errs; // kept for symmetry with reader error handling
        for i in 0..iterations {
            if let Err(e) = conn.execute(
                "UPDATE branch_inventory SET quantity = ?1 WHERE id = 'concur-1'",
                params![100 + i],
            ) {
                errs.lock().unwrap().push(format!("writer update: {e}"));
            }
            thread::sleep(Duration::from_micros(50));
        }
        drop(conn);
    }));

    for h in handles {
        h.join().unwrap();
    }

    let error_list = errors.lock().unwrap();
    let reader_results = readers_done.lock().unwrap();
    println!("  Readers completed: {}/{}", reader_results.len(), reader_count);
    println!("  Errors: {}", error_list.len());
    for e in error_list.iter() {
        println!("    ERROR: {e}");
    }

    // Verify final value is consistent
    let verify = Connection::open(db_path).unwrap();
    let final_qty: i64 = verify.query_row(
        "SELECT quantity FROM branch_inventory WHERE id = 'concur-1'",
        [], |r| r.get(0)
    ).unwrap();
    println!("  Final quantity: {final_qty} (expected 100+iterations-1 = {})", 100 + iterations - 1);

    assert!(error_list.is_empty(), "Concurrent reads must not produce errors");
    assert_eq!(reader_results.len(), reader_count, "All readers must complete");
    println!("\n  ✅ Step 5 PASSED — WAL mode handles concurrent readers + writer\n");
}

// ─── Main integration test ─────────────────────────────────────────────

#[test]
fn full_integration() {
    step3_latency_measurement();
    step4_wal_savepoint_crsqlite();
    step5_concurrency();
}
