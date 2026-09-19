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
  public inTx = false;
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
    this.inTx = false;
    this.queryLog = [];
    this.secureStore.clear();
  }

  private isIdempotentDDLError(err: any, sql: string): boolean {
    if (!err?.message) return false;
    const msg = err.message as string;
    const upper = sql.trim().toUpperCase();
    // ALTER TABLE ADD COLUMN on existing column
    if (msg.includes('duplicate column name')) return true;
    // ALTER TABLE on a table that doesn't exist yet
    if (msg.includes('no such table') && (upper.startsWith('ALTER TABLE') || upper.startsWith('CREATE INDEX') || upper.startsWith('DROP INDEX'))) return true;
    // RENAME COLUMN on a non-existent source column
    if (msg.includes('no such column') && upper.includes('RENAME COLUMN')) return true;
    // CREATE TABLE without IF NOT EXISTS when table exists
    if (msg.includes('already exists') && upper.startsWith('CREATE TABLE')) return true;
    return false;
  }

  public execute(sql: string, values: unknown[] = []) {
    this.queryLog.push({ type: 'execute', sql, values, timestamp: Date.now() });
    const { normSql, normValues } = rewriteSqlAndValues(sql, values);
    const upper = normSql.trim().toUpperCase();
    if (upper.startsWith('BEGIN')) {
      if (this.inTx) {
        return { rowsAffected: 0, lastInsertId: 0 };
      }
      this.inTx = true;
    } else if (upper.startsWith('COMMIT') || upper.startsWith('ROLLBACK')) {
      if (!this.inTx) {
        return { rowsAffected: 0, lastInsertId: 0 };
      }
      this.inTx = false;
    }
    try {
      const res = this.db.prepare(normSql).run(...normValues);
      return { rowsAffected: Number(res.changes), lastInsertId: Number(res.lastInsertRowid) };
    } catch (err: any) {
      if (upper.startsWith('BEGIN')) this.inTx = false;
      if (this.isIdempotentDDLError(err, normSql)) {
        return { rowsAffected: 0, lastInsertId: 0 };
      }
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
    // Split into individual statements so idempotent DDL errors can be swallowed
    // per-statement rather than aborting the entire batch.
    const stmts = sql.split(';').map(s => s.trim()).filter(s => s.length > 0);
    for (const stmt of stmts) {
      try {
        this.db.exec(stmt + ';');
      } catch (err: any) {
        if (this.isIdempotentDDLError(err, stmt)) continue;
        throw err;
      }
    }
  }

  public execute_transaction(statements: Array<{ sql: string; values?: unknown[]; expected_rows?: number; error_message?: string }>) {
    this.queryLog.push({ type: 'transaction', sql: statements.map(s => s.sql).join('; '), timestamp: Date.now() });
    const needsBegin = !this.inTx;
    if (needsBegin) {
      this.inTx = true;
      this.db.exec('BEGIN IMMEDIATE TRANSACTION;');
    }
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
      if (needsBegin && this.inTx) {
        this.db.exec('COMMIT;');
        this.inTx = false;
      }
      return results;
    } catch (err) {
      if (needsBegin && this.inTx) {
        try { this.db.exec('ROLLBACK;'); } catch {}
        this.inTx = false;
      }
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

  /**
   * Pre-seed the in-memory SQLite with the full v30 schema so the app's
   * runMigrations() sees user_version=30 and returns immediately (1 IPC call
   * instead of ~200), eliminating the multi-second DB init delay in tests.
   */
  public prewarmSchema(): void {
    this.db.exec(`
      PRAGMA user_version = 30;
      PRAGMA foreign_keys = ON;

      CREATE TABLE IF NOT EXISTS sync_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS drugs (
        id                            TEXT NOT NULL PRIMARY KEY,
        organization_id               TEXT NOT NULL DEFAULT '',
        name                          TEXT NOT NULL DEFAULT '',
        generic_name                  TEXT,
        brand_name                    TEXT,
        sku                           TEXT,
        barcode                       TEXT,
        category_id                   TEXT,
        drug_type                     TEXT NOT NULL DEFAULT 'otc',
        dosage_form                   TEXT,
        strength                      TEXT,
        manufacturer                  TEXT,
        supplier                      TEXT,
        requires_prescription         INTEGER NOT NULL DEFAULT 0,
        controlled_substance_schedule TEXT,
        ndc_code                      TEXT,
        unit_price                    REAL NOT NULL DEFAULT 0,
        cost_price                    REAL,
        markup_percentage             REAL,
        tax_rate                      REAL NOT NULL DEFAULT 0,
        reorder_level                 INTEGER NOT NULL DEFAULT 10,
        reorder_quantity              INTEGER NOT NULL DEFAULT 50,
        max_stock_level               INTEGER,
        unit_of_measure               TEXT NOT NULL DEFAULT 'unit',
        description                   TEXT,
        usage_instructions            TEXT,
        side_effects                  TEXT,
        contraindications             TEXT,
        storage_conditions            TEXT,
        image_url                     TEXT,
        is_active                     INTEGER NOT NULL DEFAULT 1,
        is_deleted                    INTEGER NOT NULL DEFAULT 0,
        sync_status                   TEXT NOT NULL DEFAULT 'synced',
        sync_version                  INTEGER NOT NULL DEFAULT 1,
        synced_at                     TEXT,
        updated_at                    TEXT NOT NULL DEFAULT '',
        created_at                    TEXT NOT NULL DEFAULT '',
        version_vector                TEXT NOT NULL DEFAULT '{}'
      );

      CREATE TABLE IF NOT EXISTS drug_categories (
        id              TEXT NOT NULL PRIMARY KEY,
        organization_id TEXT NOT NULL DEFAULT '',
        name            TEXT NOT NULL DEFAULT '',
        description     TEXT,
        parent_id       TEXT,
        path            TEXT,
        level           INTEGER NOT NULL DEFAULT 0,
        is_deleted      INTEGER NOT NULL DEFAULT 0,
        sync_status     TEXT NOT NULL DEFAULT 'synced',
        sync_version    INTEGER NOT NULL DEFAULT 1,
        synced_at       TEXT,
        updated_at      TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL DEFAULT '',
        version_vector  TEXT NOT NULL DEFAULT '{}'
      );

      CREATE TABLE IF NOT EXISTS price_contracts (
        id                           TEXT NOT NULL PRIMARY KEY,
        organization_id              TEXT NOT NULL DEFAULT '',
        contract_code                TEXT NOT NULL DEFAULT '',
        contract_name                TEXT NOT NULL DEFAULT '',
        contract_type                TEXT NOT NULL DEFAULT 'standard',
        is_default_contract          INTEGER NOT NULL DEFAULT 0,
        discount_type                TEXT NOT NULL DEFAULT 'percentage',
        discount_percentage          REAL NOT NULL DEFAULT 0,
        applies_to_prescription_only INTEGER NOT NULL DEFAULT 0,
        applies_to_otc               INTEGER NOT NULL DEFAULT 1,
        applies_to_all_branches      INTEGER NOT NULL DEFAULT 1,
        applicable_branch_ids        TEXT NOT NULL DEFAULT '[]',
        effective_from               TEXT NOT NULL DEFAULT '',
        effective_to                 TEXT,
        requires_verification        INTEGER NOT NULL DEFAULT 0,
        requires_approval            INTEGER NOT NULL DEFAULT 0,
        daily_usage_limit            INTEGER,
        per_customer_usage_limit     INTEGER,
        insurance_provider_id        TEXT,
        requires_preauthorization    INTEGER NOT NULL DEFAULT 0,
        minimum_purchase_amount      REAL,
        maximum_purchase_amount      REAL,
        status                       TEXT NOT NULL DEFAULT 'active',
        is_active                    INTEGER NOT NULL DEFAULT 1,
        copay_amount                 REAL,
        copay_percentage             REAL,
        is_deleted                   INTEGER NOT NULL DEFAULT 0,
        sync_status                  TEXT NOT NULL DEFAULT 'synced',
        sync_version                 INTEGER NOT NULL DEFAULT 1,
        synced_at                    TEXT,
        updated_at                   TEXT NOT NULL DEFAULT '',
        created_at                   TEXT NOT NULL DEFAULT ''
      );

      CREATE TABLE IF NOT EXISTS customers (
        id                       TEXT NOT NULL PRIMARY KEY,
        organization_id          TEXT NOT NULL DEFAULT '',
        customer_type            TEXT NOT NULL DEFAULT 'walk_in',
        first_name               TEXT,
        last_name                TEXT,
        phone                    TEXT,
        email                    TEXT,
        date_of_birth            TEXT,
        address                  TEXT,
        allergies                TEXT,
        chronic_conditions       TEXT,
        preferred_contact_method TEXT,
        marketing_consent        INTEGER NOT NULL DEFAULT 0,
        insurance_card_image_url TEXT,
        loyalty_points           INTEGER NOT NULL DEFAULT 0,
        loyalty_tier             TEXT NOT NULL DEFAULT 'bronze',
        insurance_provider_id    TEXT,
        insurance_member_id      TEXT,
        preferred_contract_id    TEXT,
        is_active                INTEGER NOT NULL DEFAULT 1,
        is_deleted               INTEGER NOT NULL DEFAULT 0,
        sync_status              TEXT NOT NULL DEFAULT 'synced',
        sync_version             INTEGER NOT NULL DEFAULT 1,
        synced_at                TEXT,
        updated_at               TEXT NOT NULL DEFAULT '',
        created_at               TEXT NOT NULL DEFAULT '',
        version_vector           TEXT NOT NULL DEFAULT '{}'
      );

      CREATE TABLE IF NOT EXISTS branch_inventory (
        id                TEXT NOT NULL PRIMARY KEY,
        branch_id         TEXT NOT NULL DEFAULT '',
        drug_id           TEXT NOT NULL DEFAULT '',
        quantity          INTEGER NOT NULL DEFAULT 0,
        reserved_quantity INTEGER NOT NULL DEFAULT 0,
        sellable_quantity INTEGER NOT NULL DEFAULT 0,
        location          TEXT,
        selling_price     REAL,
        sync_status       TEXT NOT NULL DEFAULT 'synced',
        sync_version      INTEGER NOT NULL DEFAULT 1,
        synced_at         TEXT,
        updated_at        TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL DEFAULT ''
      );

      CREATE TABLE IF NOT EXISTS drug_batches (
        id                 TEXT NOT NULL PRIMARY KEY,
        branch_id          TEXT NOT NULL DEFAULT '',
        drug_id            TEXT NOT NULL DEFAULT '',
        batch_number       TEXT NOT NULL DEFAULT '',
        quantity           INTEGER NOT NULL DEFAULT 0,
        remaining_quantity INTEGER NOT NULL DEFAULT 0,
        manufacturing_date TEXT,
        expiry_date        TEXT NOT NULL DEFAULT '',
        cost_price         REAL,
        selling_price      REAL,
        supplier           TEXT,
        purchase_order_id  TEXT,
        sync_status        TEXT NOT NULL DEFAULT 'synced',
        sync_version       INTEGER NOT NULL DEFAULT 1,
        synced_at          TEXT,
        updated_at         TEXT NOT NULL DEFAULT '',
        created_at         TEXT NOT NULL DEFAULT ''
      );

      CREATE TABLE IF NOT EXISTS sales (
        id                           TEXT NOT NULL PRIMARY KEY,
        organization_id              TEXT NOT NULL DEFAULT '',
        branch_id                    TEXT NOT NULL DEFAULT '',
        sale_number                  TEXT NOT NULL DEFAULT '',
        customer_id                  TEXT,
        customer_name                TEXT,
        subtotal                     REAL NOT NULL DEFAULT 0,
        discount_amount              REAL NOT NULL DEFAULT 0,
        tax_amount                   REAL NOT NULL DEFAULT 0,
        total_amount                 REAL NOT NULL DEFAULT 0,
        price_contract_id            TEXT,
        contract_name                TEXT,
        contract_discount_percentage REAL,
        contract_type                TEXT,
        payment_method               TEXT NOT NULL DEFAULT 'cash',
        payment_status               TEXT NOT NULL DEFAULT 'completed',
        amount_paid                  REAL,
        change_amount                REAL NOT NULL DEFAULT 0,
        payment_reference            TEXT,
        split_payment_details        TEXT,
        insurance_preauth_number     TEXT,
        prescription_id              TEXT,
        prescription_number          TEXT,
        prescriber_name              TEXT,
        prescriber_license           TEXT,
        cashier_id                   TEXT NOT NULL DEFAULT '',
        pharmacist_id                TEXT,
        insurance_claim_number       TEXT,
        patient_copay_amount         REAL,
        insurance_covered_amount     REAL,
        insurance_verified           INTEGER NOT NULL DEFAULT 0,
        insurance_verified_at        TEXT,
        insurance_verified_by        TEXT,
        notes                        TEXT,
        status                       TEXT NOT NULL DEFAULT 'completed',
        cancelled_at                 TEXT,
        cancelled_by                 TEXT,
        cancellation_reason          TEXT,
        refund_amount                REAL,
        refunded_at                  TEXT,
        refunded_by                  TEXT,
        refund_reason                TEXT,
        refund_reference             TEXT,
        receipt_printed              INTEGER NOT NULL DEFAULT 0,
        receipt_emailed              INTEGER NOT NULL DEFAULT 0,
        items_json                   TEXT NOT NULL DEFAULT '[]',
        items_count                  INTEGER NOT NULL DEFAULT 0,
        sync_status                  TEXT NOT NULL DEFAULT 'synced',
        sync_version                 INTEGER NOT NULL DEFAULT 1,
        synced_at                    TEXT,
        updated_at                   TEXT NOT NULL DEFAULT '',
        created_at                   TEXT NOT NULL DEFAULT ''
      );

      CREATE TABLE IF NOT EXISTS purchase_orders (
        id                     TEXT NOT NULL PRIMARY KEY,
        organization_id        TEXT NOT NULL DEFAULT '',
        branch_id              TEXT NOT NULL DEFAULT '',
        po_number              TEXT NOT NULL DEFAULT '',
        supplier_id            TEXT,
        subtotal               REAL NOT NULL DEFAULT 0,
        tax_amount             REAL NOT NULL DEFAULT 0,
        shipping_cost          REAL NOT NULL DEFAULT 0,
        total_amount           REAL NOT NULL DEFAULT 0,
        status                 TEXT NOT NULL DEFAULT 'draft',
        ordered_by             TEXT NOT NULL DEFAULT '',
        approved_by            TEXT,
        approved_at            TEXT,
        expected_delivery_date TEXT,
        received_date          TEXT,
        notes                  TEXT,
        items_json             TEXT NOT NULL DEFAULT '[]',
        sync_status            TEXT NOT NULL DEFAULT 'synced',
        sync_version           INTEGER NOT NULL DEFAULT 1,
        synced_at              TEXT,
        updated_at             TEXT NOT NULL DEFAULT '',
        created_at             TEXT NOT NULL DEFAULT ''
      );

      CREATE TABLE IF NOT EXISTS offline_sales (
        id                    TEXT PRIMARY KEY,
        idempotency_key       TEXT NOT NULL UNIQUE,
        sale_data             TEXT NOT NULL,
        sale_items            TEXT NOT NULL,
        inventory_updates     TEXT NOT NULL,
        recorded_at           TEXT NOT NULL,
        sync_status           TEXT NOT NULL DEFAULT 'pending',
        retry_count           INTEGER NOT NULL DEFAULT 0,
        last_retry_at         TEXT,
        next_retry_at         TEXT,
        error_message         TEXT,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL,
        crr_start_db_version  INTEGER NOT NULL DEFAULT 0
      );

      CREATE TABLE IF NOT EXISTS prescriptions (
        id                  TEXT NOT NULL PRIMARY KEY,
        organization_id     TEXT NOT NULL DEFAULT '',
        branch_id           TEXT NOT NULL DEFAULT '',
        prescription_number TEXT NOT NULL DEFAULT '',
        customer_id         TEXT NOT NULL DEFAULT '',
        prescriber_name     TEXT NOT NULL DEFAULT '',
        prescriber_license  TEXT NOT NULL DEFAULT '',
        prescriber_phone    TEXT,
        prescriber_address  TEXT,
        issue_date          TEXT NOT NULL DEFAULT '',
        expiry_date         TEXT,
        diagnosis           TEXT,
        notes               TEXT,
        medications         TEXT NOT NULL DEFAULT '[]',
        refills_allowed     INTEGER NOT NULL DEFAULT 0,
        refills_remaining   INTEGER NOT NULL DEFAULT 0,
        last_refill_date    TEXT,
        status              TEXT NOT NULL DEFAULT 'active',
        is_deleted          INTEGER NOT NULL DEFAULT 0,
        sync_status         TEXT NOT NULL DEFAULT 'synced',
        sync_version        INTEGER NOT NULL DEFAULT 1,
        synced_at           TEXT,
        updated_at          TEXT NOT NULL DEFAULT '',
        created_at          TEXT NOT NULL DEFAULT ''
      );

      CREATE TABLE IF NOT EXISTS crr_renumber_audit (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id         TEXT NOT NULL UNIQUE,
        table_name       TEXT NOT NULL,
        winner_id        TEXT NOT NULL,
        loser_id         TEXT NOT NULL,
        business_key_col TEXT NOT NULL,
        old_business_key TEXT NOT NULL,
        new_business_key TEXT NOT NULL,
        renumbered_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        uploaded_at      TEXT
      );

      CREATE TABLE IF NOT EXISTS audit_logs (
        id               TEXT NOT NULL PRIMARY KEY,
        organization_id  TEXT NOT NULL DEFAULT '',
        user_id          TEXT,
        user_full_name   TEXT,
        action           TEXT NOT NULL DEFAULT '',
        entity_type      TEXT,
        entity_id        TEXT,
        changes          TEXT,
        ip_address       TEXT,
        user_agent       TEXT,
        context_metadata TEXT,
        created_at       TEXT NOT NULL DEFAULT '',
        updated_at       TEXT NOT NULL DEFAULT '',
        sync_status      TEXT NOT NULL DEFAULT 'synced',
        sync_version     INTEGER NOT NULL DEFAULT 1,
        last_synced_at   TEXT,
        sync_hash        TEXT
      );

      CREATE TABLE IF NOT EXISTS suppressed_crr_changes (
        table_name TEXT NOT NULL,
        db_version INTEGER NOT NULL,
        record_id  TEXT NOT NULL,
        reason     TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (table_name, db_version, record_id)
      );

      CREATE TABLE IF NOT EXISTS event_outbox (
        event_id       TEXT NOT NULL PRIMARY KEY,
        aggregate_type TEXT NOT NULL,
        event_type     TEXT NOT NULL,
        aggregate_id   TEXT NOT NULL,
        org_id         TEXT NOT NULL,
        branch_id      TEXT NOT NULL,
        authored_by    TEXT NOT NULL,
        authored_at    TEXT NOT NULL,
        schema_version INTEGER NOT NULL DEFAULT 1,
        payload        TEXT NOT NULL,
        dependencies   TEXT NOT NULL DEFAULT '[]',
        hash_prev      TEXT NOT NULL,
        hash_self      TEXT NOT NULL,
        status         TEXT NOT NULL DEFAULT 'pending',
        attempts       INTEGER NOT NULL DEFAULT 0,
        error_code     TEXT,
        error_message  TEXT,
        created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
      );

      CREATE INDEX IF NOT EXISTS ix_event_outbox_status
        ON event_outbox (status, created_at);

      CREATE TABLE IF NOT EXISTS pending_conflicts (
        id               TEXT PRIMARY KEY,
        org_id           TEXT NOT NULL,
        aggregate_type   TEXT NOT NULL,
        aggregate_id     TEXT NOT NULL,
        event_id         TEXT,
        local_vector     TEXT NOT NULL DEFAULT '{}',
        local_snapshot   TEXT NOT NULL DEFAULT '{}',
        incoming_vector  TEXT NOT NULL DEFAULT '{}',
        incoming_payload TEXT NOT NULL DEFAULT '{}',
        status           TEXT NOT NULL DEFAULT 'pending',
        resolved_at      TEXT,
        created_at       TEXT NOT NULL
      );

      CREATE INDEX IF NOT EXISTS ix_pending_conflicts_status
        ON pending_conflicts (status, created_at);

      CREATE TABLE IF NOT EXISTS stock_leases (
        id                TEXT PRIMARY KEY,
        branch_id         TEXT NOT NULL,
        drug_id           TEXT NOT NULL,
        terminal_id       TEXT NOT NULL,
        leased_quantity   INTEGER NOT NULL DEFAULT 0,
        consumed_quantity INTEGER NOT NULL DEFAULT 0,
        expires_at        TEXT NOT NULL,
        status            TEXT NOT NULL DEFAULT 'active',
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
      );

      CREATE INDEX IF NOT EXISTS idx_suppressed_crr_version
        ON suppressed_crr_changes (table_name, db_version);
    `);
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
