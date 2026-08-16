import { DatabaseSync } from 'node:sqlite';

const db = new DatabaseSync(':memory:');

function execute(sql, values = []) {
    const norm = sql.replace(/\$\d+/g, '?');
    const res = db.prepare(norm).run(...values);
    return { rowsAffected: Number(res.changes), lastInsertId: Number(res.lastInsertRowid) };
}

function select(sql, values = []) {
    const norm = sql.replace(/\$\d+/g, '?');
    return db.prepare(norm).all(...values);
}

function execute_batch(sql) {
    db.exec(sql);
}

console.log("Testing SQLite bridge setup...");
execute_batch("PRAGMA foreign_keys = ON;");
execute("CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);");
execute("INSERT INTO sync_meta (key, value) VALUES ($1, $2)", ["device_id", "dev-123"]);
const rows = select("SELECT * FROM sync_meta WHERE key = $1", ["device_id"]);
console.log("Selected rows:", rows);
console.log("Bridge works!");
