import { Page, BrowserContext } from '@playwright/test';
import { DatabaseSync } from 'node:sqlite';

export interface SqliteBridgeOptions {
  dbPath?: string; // ':memory:' or file path
  onQuery?: (type: string, sql: string, values?: unknown[]) => void;
}

function rewriteSqlAndValues(sql: string, values: unknown[] = []): { normSql: string; normValues: unknown[] } {
  if (!/\$\d+/.test(sql)) {
    return { normSql: sql, normValues: values };
  }
  const normValues: unknown[] = [];
  const normSql = sql.replace(/\$(\d+)/g, (_, idxStr) => {
    const idx = parseInt(idxStr, 10) - 1;
    normValues.push(values[idx]);
    return '?';
  });
  return { normSql, normValues };
}

export class TauriSqliteBridge {
  public db: DatabaseSync;
  public queryLog: Array<{ type: string; sql: string; values?: unknown[]; timestamp: number }> = [];
  public secureStore: Map<string, any> = new Map();

  constructor(options: SqliteBridgeOptions = {}) {
    this.db = new DatabaseSync(options.dbPath || ':memory:');
    this.db.exec('PRAGMA foreign_keys = ON;');
  }

  public reset(dbPath = ':memory:') {
    try {
      this.db.close();
    } catch {
      // ignore
    }
    this.db = new DatabaseSync(dbPath);
    this.db.exec('PRAGMA foreign_keys = ON;');
    this.queryLog = [];
    this.secureStore.clear();
  }

  public execute(sql: string, values: unknown[] = []) {
    this.queryLog.push({ type: 'execute', sql, values, timestamp: Date.now() });
    const { normSql, normValues } = rewriteSqlAndValues(sql, values);
    try {
      const res = this.db.prepare(normSql).run(...normValues);
      return { rowsAffected: Number(res.changes), lastInsertId: Number(res.lastInsertRowid) };
    } catch (err: any) {
      console.error(`[BRIDGE EXECUTE ERROR] SQL: "${sql}" | Norm: "${normSql}" | Values:`, values, `| Error:`, err.message);
      throw err;
    }
  }

  public select<T = any>(sql: string, values: unknown[] = []): T[] {
    this.queryLog.push({ type: 'select', sql, values, timestamp: Date.now() });
    const { normSql, normValues } = rewriteSqlAndValues(sql, values);
    return this.db.prepare(normSql).all(...normValues) as T[];
  }

  public execute_batch(sql: string) {
    this.queryLog.push({ type: 'execute_batch', sql, timestamp: Date.now() });
    this.db.exec(sql);
  }

  public execute_transaction(statements: Array<{ sql: string; values?: unknown[]; expected_rows?: number; error_message?: string }>) {
    this.queryLog.push({ type: 'transaction', sql: statements.map(s => s.sql).join('; '), timestamp: Date.now() });
    this.db.exec('BEGIN IMMEDIATE TRANSACTION;');
    const results = [];
    try {
      for (const st of statements) {
        const { normSql, normValues } = rewriteSqlAndValues(st.sql, st.values || []);
        const res = this.db.prepare(normSql).run(...normValues);
        if (st.expected_rows != null && res.changes !== BigInt(st.expected_rows) && Number(res.changes) !== st.expected_rows) {
          throw new Error(st.error_message || `Expected ${st.expected_rows} rows affected, got ${res.changes}`);
        }
        results.push({ rowsAffected: Number(res.changes), lastInsertId: Number(res.lastInsertRowid) });
      }
      this.db.exec('COMMIT;');
      return results;
    } catch (err) {
      this.db.exec('ROLLBACK;');
      throw err;
    }
  }

  /** Expose the bridge into a Playwright page or browser context */
  public async attachToPage(page: Page) {
    await page.exposeFunction('__playwright_tauri_ipc', (cmd: string, args: Record<string, any>) => {
      switch (cmd) {
        case 'db_execute':
          return this.execute(args.sql, args.values || []);
        case 'db_select':
          return this.select(args.sql, args.values || []);
        case 'db_execute_batch':
          return this.execute_batch(args.sql);
        case 'db_execute_transaction':
          return this.execute_transaction(args.statements || []);
        case 'secure_get':
          return this.secureStore.get(args.key) ?? null;
        case 'secure_set':
          this.secureStore.set(args.key, args.value);
          return null;
        case 'secure_delete':
          this.secureStore.delete(args.key);
          return null;
        case 'plugin:store|load':
          return 1;
        case 'plugin:store|get': {
          const val = this.secureStore.has(args.key) ? this.secureStore.get(args.key) : null;
          return [val, this.secureStore.has(args.key)];
        }
        case 'plugin:store|set':
          this.secureStore.set(args.key, args.value);
          return null;
        case 'plugin:store|delete':
          this.secureStore.delete(args.key);
          return null;
        case 'plugin:store|save':
          return null;
        case 'plugin:store|clear':
          this.secureStore.clear();
          return null;
        case 'plugin:app|version':
          return '1.2.39';
        case 'plugin:app|name':
          return 'Laso Pharmacy';
        default:
          console.warn(`[TauriSqliteBridge] Unhandled command: ${cmd}`, args);
          return null;
      }
    });

    await page.addInitScript(() => {
      (window as any).__TAURI_INTERNALS__ = {
        invoke: async (cmd: string, args: any) => {
          return await (window as any).__playwright_tauri_ipc(cmd, args);
        },
      };
      // Polyfill basic tauri storage / store plugin if called
      (window as any).__TAURI__ = {
        core: {
          invoke: async (cmd: string, args: any) => {
            return await (window as any).__playwright_tauri_ipc(cmd, args);
          },
        },
      };
    });
  }

  // Inspection helpers
  public getTableRows(tableName: string) {
    try {
      return this.select(`SELECT * FROM ${tableName}`);
    } catch (e) {
      return [];
    }
  }

  public getPendingOutboxCount() {
    try {
      const rows = this.select<{ count: number }>("SELECT COUNT(*) as count FROM event_outbox WHERE status IN ('pending', 'failed', 'accepted_deferred')");
      return rows[0]?.count ?? 0;
    } catch {
      return 0;
    }
  }

  public getOutboxCount() {
    return this.getPendingOutboxCount();
  }

  public getLastSyncAt(branchId?: string) {
    try {
      const key = branchId ? `last_sync_at:${branchId}` : 'last_sync_at';
      const rows = this.select<{ value: string }>('SELECT value FROM sync_meta WHERE key = ? OR key LIKE ? ORDER BY key DESC LIMIT 1', [key, `${key}%`]);
      return rows[0]?.value ?? null;
    } catch {
      return null;
    }
  }
}
