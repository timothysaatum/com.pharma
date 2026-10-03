import { Pool } from 'pg';

/** Database names that must never be used by a test run. */
const FORBIDDEN_DATABASES = new Set(['atlasdb', 'postgres', 'template0', 'template1']);

/**
 * Resolve the connection string this helper will use, and refuse anything unsafe.
 *
 * Two failure modes are closed here:
 *
 * 1. There used to be no default and no check: with TEST_DATABASE_URL unset the
 *    helper silently connected to the PRODUCTION database (atlasdb). Every test
 *    that constructs BackendDatabase without an argument therefore talked to
 *    live pharmacy data, and any write it performed hit production.
 * 2. Even with the variable set, it could still be pointed at production.
 *
 * So: no default, and an explicit refusal for the databases a test must never
 * touch. `postgres` is refused too because a stray DROP against the maintenance
 * database is unrecoverable.
 */
export function resolveTestDatabaseUrl(explicit?: string): string {
  const url = explicit ?? process.env.TEST_DATABASE_URL;

  if (!url) {
    throw new Error(
      'TEST_DATABASE_URL is not set.\n' +
        'These tests must never fall back to a real database, so there is no default.\n' +
        'Point it at a disposable cluster, for example:\n' +
        '  TEST_DATABASE_URL=postgresql://laso@localhost:5432/laso_test\n' +
        'Do NOT point it at atlasdb.'
    );
  }

  let database: string;
  let host: string;
  try {
    const parsed = new URL(url);
    host = parsed.hostname;
    database = decodeURIComponent(parsed.pathname.replace(/^\//, ''));
  } catch {
    throw new Error(
      `TEST_DATABASE_URL is not a parseable URL: ${url}\n` +
        'Expected something like postgresql://user@host:5432/laso_test'
    );
  }

  if (FORBIDDEN_DATABASES.has(database.toLowerCase())) {
    throw new Error(
      `Refusing to run against database "${database}" (host ${host}).\n` +
        'These are production or maintenance databases. Running a test suite ' +
        'against atlasdb issues DROP SCHEMA ... CASCADE and destroys real data.\n' +
      'Create a disposable database instead, e.g.:\n' +
        '  createdb laso_test\n' +
        '  TEST_DATABASE_URL=postgresql://laso@localhost:5432/laso_test'
    );
  }

  if (!database) {
    throw new Error(`TEST_DATABASE_URL names no database: ${url}`);
  }

  return url;
}

export class BackendDatabase {
  private pool: Pool;

  constructor(connectionString?: string) {
    this.pool = new Pool({ connectionString: resolveTestDatabaseUrl(connectionString) });
  }

  async query<T = any>(sql: string, params: any[] = []): Promise<T[]> {
    const res = await this.pool.query(sql, params);
    return res.rows as T[];
  }

  async execute(sql: string, params: any[] = []): Promise<number> {
    const res = await this.pool.query(sql, params);
    return res.rowCount ?? 0;
  }

  async close() {
    await this.pool.end();
  }

  /** Drop the event_log rows this helper created. Never touches other rows. */
  async deleteEventsWhere(eventIds: string[]): Promise<number> {
    if (eventIds.length === 0) return 0;
    // Scoped by explicit event_id. This helper never issues an unqualified
    // DELETE FROM event_log: event_log is the audit trail and rows are only
    // removable when the caller knows exactly which ones it created.
    const res = await this.pool.query(
      'DELETE FROM event_log WHERE event_id = ANY($1::text[])',
      [eventIds]
    );
    return res.rowCount ?? 0;
  }

  async getEvents(orgId: string, afterSeq = 0) {
    return this.query(
      'SELECT seq, event_id, event_type, aggregate_type, aggregate_id, payload, hash_self, hash_prev FROM event_log WHERE org_id = $1 AND seq > $2 ORDER BY seq ASC',
      [orgId, afterSeq]
    );
  }

  async getBranchInventory(branchId: string) {
    return this.query(
      `SELECT bi.id, bi.branch_id, bi.drug_id, bi.quantity, bi.location, bi.selling_price, d.name as drug_name, d.sku
       FROM branch_inventory bi
       JOIN drugs d ON d.id = bi.drug_id
       WHERE bi.branch_id = $1
       ORDER BY d.name ASC`,
      [branchId]
    );
  }

  async getDrugBatches(branchId: string) {
    return this.query(
      `SELECT db.id, db.branch_id, db.drug_id, db.batch_number, db.quantity, db.remaining_quantity, db.expiry_date, d.name as drug_name
       FROM drug_batches db
       JOIN drugs d ON d.id = db.drug_id
       WHERE db.branch_id = $1
       ORDER BY db.batch_number ASC`,
      [branchId]
    );
  }

  async getSales(orgId: string) {
    return this.query(
      'SELECT id, sale_number, total_amount, payment_method, status, customer_id FROM sales WHERE organization_id = $1 ORDER BY created_at DESC',
      [orgId]
    );
  }

  async seedDefaultPriceContract(orgId: string) {
    // Check if a default contract already exists for this org
    const existing = await this.query<{ id: string }>(
      `SELECT id FROM price_contracts WHERE organization_id = $1 AND is_default_contract = TRUE LIMIT 1`,
      [orgId]
    );
    if (existing.length > 0) {
      return existing[0].id;
    }

    const contractId = '33333333-3333-3333-3333-333333333333';
    await this.query(
      `INSERT INTO price_contracts (
        id, organization_id, contract_code, contract_name, contract_type,
        is_default_contract, discount_type, discount_percentage,
        applies_to_prescription_only, applies_to_otc, excluded_drug_categories, excluded_drug_ids,
        applies_to_all_branches, applicable_branch_ids, effective_from,
        requires_verification, allowed_user_roles, requires_approval, requires_preauthorization,
        status, is_active, total_transactions, total_discount_given,
        created_by, created_at, updated_at, sync_version, sync_status, is_deleted
      ) VALUES (
        $1, $2, 'STD-001', 'Standard Retail', 'standard',
        TRUE, 'percentage', 0.0,
        FALSE, TRUE, '[]', '[]',
        TRUE, '[]', NOW(),
        FALSE, '["admin", "manager", "cashier", "pharmacist"]', FALSE, FALSE,
        'active', TRUE, 0, 0.0,
        '44444444-4444-4444-4444-444444444444', NOW(), NOW(), 1, 'synced', FALSE
      ) ON CONFLICT (id) DO UPDATE SET is_active = TRUE, is_deleted = FALSE`,
      [contractId, orgId]
    );
    return contractId;
  }

  async seedDrugAndStock(params: {
    org_id: string;
    branch_id: string;
    drug_id: string;
    name: string;
    unit_price: number;
    quantity: number;
    batch_number?: string;
  }) {
    const crypto = await import('crypto');
    const batchId = crypto.randomUUID();
    const batchNumber = params.batch_number || `BATCH-${Date.now().toString().slice(-4)}`;

    // 1. Insert drug in PG
    await this.query(
      `INSERT INTO drugs (
        id, organization_id, name, drug_type, unit_price, tax_rate, reorder_level, reorder_quantity,
        unit_of_measure, version_vector, requires_prescription,
        is_active, is_deleted, created_at, updated_at, sync_version, sync_status
      ) VALUES ($1, $2, $3, 'otc', $4, 0.0, 0, 0, 'unit', '{}', FALSE, TRUE, FALSE, NOW(), NOW(), 1, 'synced')
      ON CONFLICT (id) DO UPDATE SET unit_price = $4, is_active = TRUE, is_deleted = FALSE`,
      [params.drug_id, params.org_id, params.name, params.unit_price]
    );

    // 2. Insert branch_inventory in PG
    await this.query(
      `INSERT INTO branch_inventory (
        id, branch_id, drug_id, quantity, reserved_quantity, selling_price, version_id, created_at, updated_at, sync_version, sync_status
      ) VALUES (gen_random_uuid(), $1, $2, $3, 0, $4, 1, NOW(), NOW(), 1, 'synced')
      ON CONFLICT (id) DO NOTHING`,
      [params.branch_id, params.drug_id, params.quantity, params.unit_price]
    );

    // 3. Insert drug_batch in PG
    await this.query(
      `INSERT INTO drug_batches (
        id, branch_id, drug_id, batch_number, quantity, remaining_quantity,
        expiry_date, selling_price, cost_price, version_id, created_at, updated_at, sync_version, sync_status
      ) VALUES ($1, $2, $3, $4, $5, $5, '2028-12-31', $6, 5.00, 1, NOW(), NOW(), 1, 'synced')
      ON CONFLICT (id) DO NOTHING`,
      [batchId, params.branch_id, params.drug_id, batchNumber, params.quantity, params.unit_price]
    );

    return { drug_id: params.drug_id, batch_id: batchId, batch_number: batchNumber };
  }

  async getCustomers(orgId: string) {
    return this.query(
      'SELECT id, name, phone, email, customer_type FROM customers WHERE organization_id = $1 ORDER BY created_at DESC',
      [orgId]
    );
  }

  async insertServerEvent(envelope: {
    org_id: string;
    aggregate_id: string;
    aggregate_type: string;
    event_type: string;
    payload: any;
    branch_id?: string;
  }) {
    const crypto = await import('crypto');
    const ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
    const timePart = Date.now().toString(32).toUpperCase().padStart(10, '0');
    const randChars = Array.from(crypto.randomBytes(16)).map(b => ALPHABET[b % 32]).join('');
    const event_id = (timePart + randChars).slice(0, 26);

    const branch_id = envelope.branch_id || '22222222-2222-2222-2222-222222222222';

    const lastRow = await this.query<{ hash_self: string; max_seq: number }>(
      'SELECT hash_self, seq as max_seq FROM event_log WHERE org_id = $1 ORDER BY seq DESC LIMIT 1',
      [envelope.org_id]
    );
    const hash_prev = lastRow[0]?.hash_self || '0000000000000000000000000000000000000000000000000000000000000000';
    const next_seq = Number(lastRow[0]?.max_seq ?? 0) + 1;
    const hash_self = crypto.createHash('sha256').update(hash_prev + JSON.stringify(envelope.payload)).digest('hex');

    const res = await this.query(
      `INSERT INTO event_log (
        event_id, org_id, seq, aggregate_id, aggregate_type, event_type, schema_version,
        payload, dependencies, authored_at, authored_by, branch_id, hash_self, hash_prev, received_at
      ) VALUES ($1, $2, $3, $4, $5, $6, 1, $7, '{}'::text[], NOW(), '44444444-4444-4444-4444-444444444444'::uuid, $8, $9, $10, NOW())
      RETURNING seq, event_id`,
      [
        event_id,
        envelope.org_id,
        next_seq,
        envelope.aggregate_id,
        envelope.aggregate_type,
        envelope.event_type,
        JSON.stringify(envelope.payload),
        branch_id,
        hash_self,
        hash_prev,
      ]
    );

    // Apply customer read projection to PostgreSQL
    if (envelope.event_type === 'customer_created') {
      const p = envelope.payload;
      await this.query(
        `INSERT INTO customers (
          id, organization_id, customer_type, first_name, last_name, phone, email,
          loyalty_points, loyalty_tier, preferred_contact_method, marketing_consent, is_active,
          allergies, chronic_conditions,
          version_vector, sync_version, sync_status, is_deleted, created_at, updated_at,
          total_orders, total_value, medical_data_encrypted
        ) VALUES (
          $1, $2, $3, $4, $5, $6, $7,
          $8, $9, $10, $11, $12,
          '[]', '[]',
          $13, 1, 'synced', FALSE, NOW(), NOW(),
          0, 0.0, FALSE
        ) ON CONFLICT (id) DO NOTHING`,
        [
          envelope.aggregate_id,
          envelope.org_id,
          p.customer_type || 'registered',
          p.first_name || '',
          p.last_name || '',
          p.phone || null,
          p.email || null,
          p.loyalty_points || 0,
          p.loyalty_tier || 'bronze',
          p.preferred_contact_method || 'email',
          p.marketing_consent || false,
          p.is_active !== undefined ? p.is_active : true,
          JSON.stringify(p.version_vector || {}),
        ]
      );
    } else if (envelope.event_type === 'customer_updated') {
      const p = envelope.payload;
      await this.query(
        `UPDATE customers SET
          first_name = COALESCE($3, first_name),
          last_name = COALESCE($4, last_name),
          version_vector = COALESCE($5, version_vector),
          updated_at = NOW()
        WHERE id = $1 AND organization_id = $2`,
        [
          envelope.aggregate_id,
          envelope.org_id,
          p.first_name || null,
          p.last_name || null,
          p.version_vector ? JSON.stringify(p.version_vector) : null,
        ]
      );
    }

    return res[0];
  }
}
