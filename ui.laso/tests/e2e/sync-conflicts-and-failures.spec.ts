import { test, expect } from '@playwright/test';
import { setupAuthenticatedState } from './helpers/auth-helper';
import { TauriSqliteBridge } from './helpers/tauri-bridge';
import { BackendDatabase } from './helpers/backend-db';

test.describe('Sync Conflicts, Idempotency & Terminal Failures E2E Tests', () => {
  let bridge: TauriSqliteBridge;
  let backendDb: BackendDatabase;
  const orgId = '11111111-1111-1111-1111-111111111111';
  const branchId = '22222222-2222-2222-2222-222222222222';
  const userId = '44444444-4444-4444-4444-444444444444';

  test.beforeAll(async () => {
    backendDb = new BackendDatabase();
  });

  test.afterAll(async () => {
    await backendDb.close();
  });

  test.beforeEach(async ({ page }) => {
    bridge = new TauriSqliteBridge();
    await bridge.attachToPage(page);
    await setupAuthenticatedState(page, bridge);
  });

  test('1. Concurrent edits trigger version vector conflict and park in unresolved_conflicts', async ({ page }) => {
    await page.goto('/customers');
    await page.waitForLoadState('domcontentloaded');

    const custId = '66666666-6666-6666-6666-' + Math.random().toString(16).substring(2, 14).padEnd(12, '0');
    const initialName = `ConflictCust${Date.now().toString().slice(-4)}`;

    // 1. Insert customer on server with vector {"server-site": 1}
    await backendDb.insertServerEvent({
      org_id: orgId,
      aggregate_id: custId,
      aggregate_type: 'customer',
      event_type: 'customer_created',
      payload: {
        id: custId,
        organization_id: orgId,
        first_name: initialName,
        last_name: 'Initial',
        customer_type: 'registered',
        phone: '+23324' + Math.floor(1000000 + Math.random() * 9000000),
        version_vector: { 'server-site': 1 },
      },
    });

    // Sync down to client
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });

    // 2. Server makes a concurrent edit with vector {"server-site": 2}
    await backendDb.insertServerEvent({
      org_id: orgId,
      aggregate_id: custId,
      aggregate_type: 'customer',
      event_type: 'customer_updated',
      payload: {
        id: custId,
        first_name: `${initialName}-ServerWon`,
        version_vector: { 'server-site': 2 },
      },
    });

    // 3. Client writes an offline edit with vector {"client-site": 1}
    await page.evaluate(async ({ custId, orgId, branchId, initialName }) => {
      // @ts-ignore
      const { writeLocal } = await import('/src/lib/localWrite.ts');
      const now = new Date().toISOString();
      await writeLocal.customer({
        id: custId,
        organization_id: orgId,
        customer_type: 'registered',
        first_name: `${initialName}-ClientBranch`,
        last_name: 'Initial',
        phone: '+233241112233',
        email: null,
        date_of_birth: null,
        loyalty_points: 0,
        loyalty_tier: 'bronze',
        preferred_contact_method: 'email',
        marketing_consent: false,
        is_active: true,
        insurance_provider_id: null,
        insurance_member_id: null,
        insurance_card_image_url: null,
        preferred_contract_id: null,
        allergies: [],
        chronic_conditions: [],
        is_deleted: false,
        deleted_at: null,
        deleted_by: null,
        sync_status: 'pending',
        sync_version: 1,
        synced_at: null,
        created_at: now,
        updated_at: now,
        insurance_provider_name: null,
        insurance_provider_code: null,
        preferred_contract_name: null,
        preferred_contract_discount: null,
        total_purchases: 0,
        total_spent: 0,
        total_orders: 0,
        total_value: 0,
        last_purchase_date: null,
      }, 'update', branchId);
    }, { custId, orgId, branchId, initialName });

    // 4. Check outbox
    const beforeSyncOutbox = bridge.select<{ event_id: string; status: string; payload: string }>(
      'SELECT event_id, status, payload FROM event_outbox'
    );
    console.log('Test 1 Outbox Before Sync:', beforeSyncOutbox);

    // Trigger sync on client
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });

    const afterSyncOutbox = bridge.select<{ event_id: string; status: string; error_code: string }>(
      'SELECT event_id, status, error_code FROM event_outbox'
    );
    console.log('Test 1 Outbox After Sync:', afterSyncOutbox);

    // 5. Verify conflict is parked in server's unresolved_conflicts table
    await expect.poll(async () => {
      const conflicts = await backendDb.query(
        'SELECT id, aggregate_id, aggregate_type, status, local_vector, incoming_vector FROM unresolved_conflicts WHERE org_id = $1 AND aggregate_id = $2',
        [orgId, custId]
      );
      if (conflicts.length > 0) {
        console.log('Found conflict row in DB:', conflicts[0]);
      }
      return conflicts.length;
    }, { timeout: 15000 }).toBe(1);
  });

  test('2. Push request idempotency guarantees duplicate submissions do not duplicate rows or fail', async ({ page }) => {
    await page.goto('/customers');
    await page.waitForLoadState('domcontentloaded');

    const custId = '77777777-7777-7777-7777-' + Math.random().toString(16).substring(2, 14).padEnd(12, '0');
    const firstName = `Idempotent${Date.now().toString().slice(-4)}`;

    // Write customer to outbox
    await page.evaluate(async ({ custId, orgId, branchId, firstName }) => {
      // @ts-ignore
      const { writeLocal } = await import('/src/lib/localWrite.ts');
      const now = new Date().toISOString();
      await writeLocal.customer({
        id: custId,
        organization_id: orgId,
        customer_type: 'registered',
        first_name: firstName,
        last_name: 'Test',
        phone: '+23324' + Math.floor(1000000 + Math.random() * 9000000),
        email: null,
        date_of_birth: null,
        loyalty_points: 0,
        loyalty_tier: 'bronze',
        preferred_contact_method: 'email',
        marketing_consent: false,
        is_active: true,
        insurance_provider_id: null,
        insurance_member_id: null,
        insurance_card_image_url: null,
        preferred_contract_id: null,
        allergies: [],
        chronic_conditions: [],
        is_deleted: false,
        deleted_at: null,
        deleted_by: null,
        sync_status: 'pending',
        sync_version: 1,
        synced_at: null,
        created_at: now,
        updated_at: now,
        insurance_provider_name: null,
        insurance_provider_code: null,
        preferred_contract_name: null,
        preferred_contract_discount: null,
        total_purchases: 0,
        total_spent: 0,
        total_orders: 0,
        total_value: 0,
        last_purchase_date: null,
      }, 'create', branchId);
    }, { custId, orgId, branchId, firstName });

    // Sync first time
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });

    // Verify outbox drained
    await expect.poll(() => bridge.getOutboxCount(), { timeout: 25000 }).toBe(0);

    // Get event details from outbox
    const events = bridge.select<{ event_id: string; payload: string; hash_self: string; hash_prev: string; authored_at: string }>(
      'SELECT event_id, payload, hash_self, hash_prev, authored_at FROM event_outbox WHERE aggregate_id = ?',
      [custId]
    );
    expect(events.length).toBe(1);

    // Simulate duplicate POST request directly to server
    const response = await page.evaluate(async ({ event, orgId, branchId, custId, firstName }) => {
      // @ts-ignore
      const { syncApi } = await import('/src/api/sync.ts');
      return await syncApi.pushEvents({
        branch_id: branchId,
        client_clock: new Date().toISOString(),
        events: [{
          event_id: event.event_id,
          aggregate_id: custId,
          aggregate_type: 'customer',
          event_type: 'customer_created',
          schema_version: 1,
          payload: JSON.parse(event.payload),
          dependencies: [],
          authored_at: event.authored_at,
          authored_by: orgId,
          branch_id: branchId,
          org_id: orgId,
          hash_self: event.hash_self,
          hash_prev: event.hash_prev,
        }],
      });
    }, { event: events[0], orgId, branchId, custId, firstName });

    // Verify duplicate is accepted with no error
    expect(response.results.length).toBe(1);
    expect(response.results[0].status).toBe('accepted');

    // Verify exactly 1 customer record exists in PostgreSQL (no duplication)
    const dbRows = await backendDb.query(
      'SELECT id, first_name FROM customers WHERE organization_id = $1 AND id = $2',
      [orgId, custId]
    );
    expect(dbRows.length).toBe(1);
  });

  test('3. Permanent rejection of corrupted payload marks outbox rejected without crashing sync engine', async ({ page }) => {
    await page.goto('/customers');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.locator('button:has-text("Register Customer")').first()).toBeVisible({ timeout: 15000 });

    await page.evaluate(async () => {
      // @ts-ignore
      const { getDb } = await import('/src/lib/localDb.ts');
      await getDb();
    });

    // Insert an invalid event directly into outbox with invalid hash_self to trigger permanent rejection
    const badEventId = '01K03CORRVPT00000000000000';
    const badCustId = '88888888-8888-8888-8888-888888888888';
    const now = new Date().toISOString();

    bridge.execute(
      `INSERT INTO event_outbox
        (event_id, aggregate_type, event_type, aggregate_id, org_id, branch_id, authored_by, authored_at, schema_version, payload, dependencies, hash_prev, hash_self, status)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [
        badEventId,
        'customer',
        'customer_created',
        badCustId,
        orgId,
        branchId,
        userId,
        now,
        1,
        JSON.stringify({
          organization_id: orgId,
          customer_type: 'registered',
          first_name: 'Corrupt',
          last_name: 'Test',
          phone: '+233241112233',
        }),
        '[]',
        '0'.repeat(64),
        'bad0000000000000000000000000000000000000000000000000000000000000', // valid hex but mismatched hash
        'pending',
      ]
    );

    expect(bridge.getOutboxCount()).toBeGreaterThanOrEqual(1);

    // Trigger sync
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });

    // Verify the corrupted event is marked rejected_permanent in outbox
    const badEvent = bridge.select<{ status: string; error_code: string }>(
      'SELECT status, error_code FROM event_outbox WHERE event_id = ?',
      [badEventId]
    );
    expect(badEvent.length).toBe(1);
    expect(badEvent[0].status).toBe('rejected_permanent');
    expect(badEvent[0].error_code).toBe('hash_mismatch');
  });
});
