import { test, expect } from '@playwright/test';
import { TauriSqliteBridge } from './helpers/tauri-bridge';
import { setupAuthenticatedState } from './helpers/auth-helper';

test.describe('Event-Sourced Sync: Trigger Cleanup, Projector Execution & Cursor Recovery', () => {
  let bridge: TauriSqliteBridge;
  const consoleErrors: string[] = [];
  const consoleWarnings: string[] = [];

  test.beforeEach(async ({ page }) => {
    test.setTimeout(60000);
    consoleErrors.length = 0;
    consoleWarnings.length = 0;

    page.on('console', msg => {
      const text = msg.text();
      if (msg.type() === 'error') consoleErrors.push(text);
      if (msg.type() === 'warning') consoleWarnings.push(text);
      console.log(`[BROWSER ${msg.type()}]:`, text);
    });
    page.on('pageerror', err => {
      consoleErrors.push(err.message);
      console.error('[BROWSER ERROR]:', err);
    });

    bridge = new TauriSqliteBridge();
    await bridge.attachToPage(page);
    await setupAuthenticatedState(page, bridge);
  });

  test('1. Migration v30 strips orphaned CR-SQLite triggers and leaves schema clean', async ({ page }) => {
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.getByRole('heading', { name: /Point of Sale/i })).toBeVisible({ timeout: 15000 });

    await page.evaluate(async () => {
      // @ts-ignore
      const { getDb } = await import('/src/lib/localDb.ts');
      await getDb();
    });

    // Inspect sqlite_master for triggers
    const triggers = bridge.select<{ name: string; sql: string }>(
      "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
    );

    const crrTriggers = triggers.filter(
      t => (t.name || '').includes('__crsql_') ||
           (t.name || '').startsWith('crsql_') ||
           (t.sql || '').includes('crsql_internal_sync_bit') ||
           (t.sql || '').includes('crsql_after_')
    );

    expect(crrTriggers.length).toBe(0);

    // Verify user_version is 30
    const userVersion = bridge.select<{ user_version: number }>('PRAGMA user_version');
    expect(userVersion[0].user_version).toBeGreaterThanOrEqual(30);
  });

  test('2. Inbound event pull projects drug_category_created, drug_created, and drug_updated without crsql errors', async ({ page }) => {
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.getByRole('heading', { name: /Point of Sale/i })).toBeVisible({ timeout: 15000 });

    await page.evaluate(async () => {
      // @ts-ignore
      const { getDb } = await import('/src/lib/localDb.ts');
      await getDb();
    });

    const orgId = '11111111-1111-1111-1111-111111111111';
    const branchId = '22222222-2222-2222-2222-222222222222';
    const categoryId = crypto.randomUUID();
    const drugId = crypto.randomUUID();
    const now = new Date().toISOString();

    // 1. Category Created Event
    const catEvent = {
      event_id: '01K04CAT000000000000000001',
      org_id: orgId,
      branch_id: branchId,
      authored_by: '00000000-0000-0000-0000-000000000000',
      authored_at: now,
      aggregate_id: categoryId,
      aggregate_type: 'drug_category',
      event_type: 'drug_category_created',
      schema_version: 1,
      payload: {
        organization_id: orgId,
        name: `Antibiotics-${Date.now().toString().slice(-4)}`,
        description: 'Antibacterial medicines',
        parent_id: null,
        level: 0,
      },
      dependencies: [],
      hash_self: '0'.repeat(64),
      hash_prev: '0'.repeat(64),
    };

    // 2. Drug Created Event
    const drugCreatedEvent = {
      event_id: '01K04DRG000000000000000002',
      org_id: orgId,
      branch_id: branchId,
      authored_by: '00000000-0000-0000-0000-000000000000',
      authored_at: now,
      aggregate_id: drugId,
      aggregate_type: 'drug',
      event_type: 'drug_created',
      schema_version: 1,
      payload: {
        organization_id: orgId,
        name: `Amoxicillin ${Date.now().toString().slice(-4)}`,
        generic_name: 'Amoxicillin',
        category_id: categoryId,
        drug_type: 'otc',
        unit_price: 15.50,
        cost_price: 10.00,
        tax_rate: 0.00,
        reorder_level: 10,
        reorder_quantity: 50,
        is_active: true,
        updated_at: now,
      },
      dependencies: [],
      hash_self: '0'.repeat(64),
      hash_prev: '0'.repeat(64),
    };

    // 3. Drug Updated Event (like the exact failing trace C8DC8BCA379D4EEF99E1D49B68)
    const drugUpdatedEvent = {
      event_id: 'C8DC8BCA379D4EEF99E1D49B68',
      org_id: orgId,
      branch_id: branchId,
      authored_by: '00000000-0000-0000-0000-000000000000',
      authored_at: now,
      aggregate_id: drugId,
      aggregate_type: 'drug',
      event_type: 'drug_updated',
      schema_version: 1,
      payload: {
        organization_id: orgId,
        name: `Amoxicillin ${Date.now().toString().slice(-4)} Forte`,
        generic_name: 'Amoxicillin Trihydrate',
        category_id: categoryId,
        drug_type: 'prescription',
        unit_price: 19.99,
        cost_price: 12.00,
        tax_rate: 0.00,
        reorder_level: 15,
        reorder_quantity: 60,
        is_active: true,
        updated_at: now,
      },
      dependencies: [],
      hash_self: '0'.repeat(64),
      hash_prev: '0'.repeat(64),
    };

    // Apply events via applyEventLocally in browser runtime
    await page.evaluate(async ({ catEvent, drugCreatedEvent, drugUpdatedEvent }) => {
      // @ts-ignore
      const { applyEventLocally } = await import('/src/lib/localProjectors.ts');
      // @ts-ignore
      await applyEventLocally(catEvent);
      // @ts-ignore
      await applyEventLocally(drugCreatedEvent);
      // @ts-ignore
      await applyEventLocally(drugUpdatedEvent);
    }, { catEvent, drugCreatedEvent, drugUpdatedEvent });

    // Verify local SQLite has the updated drug
    const localDrugs = bridge.select<{ id: string; name: string; unit_price: number; drug_type: string }>(
      'SELECT id, name, unit_price, drug_type FROM drugs WHERE id = ?',
      [drugId]
    );

    expect(localDrugs.length).toBe(1);
    expect(localDrugs[0].name).toContain('Forte');
    expect(localDrugs[0].unit_price).toBe(19.99);
    expect(localDrugs[0].drug_type).toBe('prescription');

    // Verify LocalRead searchDrugs returns the updated drug
    const searchResult = await page.evaluate(async (drugId) => {
      // @ts-ignore
      const { localRead } = await import('/src/lib/localRead.ts');
      return await localRead.searchDrugs({ search: 'Amoxicillin' });
    }, drugId);

    expect(searchResult.items.some((d: any) => d.id === drugId)).toBe(true);

    // Verify NO "crsql_internal_sync_bit" errors occurred
    const crsqlErrors = consoleErrors.filter(e => e.includes('crsql_internal_sync_bit'));
    expect(crsqlErrors.length).toBe(0);
  });

  test('3. SyncEngine pullEvents maintains contiguous cursor and halts without advancing on projection error', async ({ page }) => {
    const orgId = '11111111-1111-1111-1111-111111111111';
    const branchId = '22222222-2222-2222-2222-222222222222';
    const drugId = crypto.randomUUID();
    const now = new Date().toISOString();

    let failProjection = true;

    // Route /api/v1/sync/events to return a controlled batch of 3 events
    await page.route('**/api/v1/sync/events*', async (route) => {
      const url = new URL(route.request().url());
      const afterSeq = parseInt(url.searchParams.get('after_seq') || '0', 10);

      if (afterSeq === 0) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            server_clock: now,
            events: [
              {
                event_id: '01K04TEST0000000000000001',
                seq: 1,
                org_id: orgId,
                branch_id: branchId,
                authored_by: '00000000-0000-0000-0000-000000000000',
                authored_at: now,
                aggregate_id: drugId,
                aggregate_type: 'drug',
                event_type: 'drug_created',
                schema_version: 1,
                payload: {
                  organization_id: orgId,
                  name: 'Test Drug Seq 1',
                  drug_type: 'otc',
                  unit_price: 10.0,
                  is_active: true,
                },
                dependencies: [],
                hash_self: '0'.repeat(64),
                hash_prev: '0'.repeat(64),
              },
              {
                event_id: '01K04TEST0000000000000002',
                seq: 2,
                org_id: orgId,
                branch_id: branchId,
                authored_by: '00000000-0000-0000-0000-000000000000',
                authored_at: now,
                aggregate_id: drugId,
                aggregate_type: 'drug',
                event_type: 'drug_updated',
                schema_version: 1,
                payload: failProjection ? (null as any) : {
                  organization_id: orgId,
                  name: 'Test Drug Seq 2 Updated',
                  drug_type: 'otc',
                  unit_price: 12.0,
                  is_active: true,
                },
                dependencies: [],
                hash_self: '0'.repeat(64),
                hash_prev: '0'.repeat(64),
              },
              {
                event_id: '01K04TEST0000000000000003',
                seq: 3,
                org_id: orgId,
                branch_id: branchId,
                authored_by: '00000000-0000-0000-0000-000000000000',
                authored_at: now,
                aggregate_id: drugId,
                aggregate_type: 'drug',
                event_type: 'drug_updated',
                schema_version: 1,
                payload: {
                  organization_id: orgId,
                  name: 'Test Drug Seq 3 Updated',
                  drug_type: 'otc',
                  unit_price: 15.0,
                  is_active: true,
                },
                dependencies: [],
                hash_self: '0'.repeat(64),
                hash_prev: '0'.repeat(64),
              },
            ],
            has_more: false,
            next_after_seq: 3,
          }),
        });
      } else if (afterSeq === 1) {
        // Returned when retrying from after_seq = 1
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            server_clock: now,
            events: [
              {
                event_id: '01K04TEST0000000000000002',
                seq: 2,
                org_id: orgId,
                branch_id: branchId,
                authored_by: '00000000-0000-0000-0000-000000000000',
                authored_at: now,
                aggregate_id: drugId,
                aggregate_type: 'drug',
                event_type: 'drug_updated',
                schema_version: 1,
                payload: failProjection ? (null as any) : {
                  organization_id: orgId,
                  name: 'Test Drug Seq 2 Updated Fixed',
                  drug_type: 'otc',
                  unit_price: 12.0,
                  is_active: true,
                },
                dependencies: [],
                hash_self: '0'.repeat(64),
                hash_prev: '0'.repeat(64),
              },
              {
                event_id: '01K04TEST0000000000000003',
                seq: 3,
                org_id: orgId,
                branch_id: branchId,
                authored_by: '00000000-0000-0000-0000-000000000000',
                authored_at: now,
                aggregate_id: drugId,
                aggregate_type: 'drug',
                event_type: 'drug_updated',
                schema_version: 1,
                payload: {
                  organization_id: orgId,
                  name: 'Test Drug Seq 3 Final',
                  drug_type: 'otc',
                  unit_price: 15.0,
                  is_active: true,
                },
                dependencies: [],
                hash_self: '0'.repeat(64),
                hash_prev: '0'.repeat(64),
              },
            ],
            has_more: false,
            next_after_seq: 3,
          }),
        });
      } else {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            server_clock: now,
            events: [],
            has_more: false,
            next_after_seq: afterSeq,
          }),
        });
      }
    });

    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.getByRole('heading', { name: /Point of Sale/i })).toBeVisible({ timeout: 15000 });

    await page.evaluate(async () => {
      // @ts-ignore
      const { getDb, setEventPullSeq } = await import('/src/lib/localDb.ts');
      await getDb();
      await setEventPullSeq(0);
    });

    // 1. First sync pass: event 1 succeeds, event 2 fails -> cursor must stay at 1!
    await page.evaluate(async ({ branchId, orgId }) => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      // @ts-ignore
      syncEngine.branchId = branchId;
      // @ts-ignore
      syncEngine.organizationId = orgId;
      // @ts-ignore
      await syncEngine.pullEvents();
    }, { branchId, orgId });

    const pullSeqAfterFailure = await page.evaluate(async () => {
      // @ts-ignore
      const { getEventPullSeq } = await import('/src/lib/localDb.ts');
      return await getEventPullSeq();
    });

    // PROOF: Cursor did NOT advance to 3! It stayed at 1.
    expect(pullSeqAfterFailure).toBe(1);

    // 2. Second sync pass: resolve failure condition and trigger pullEvents again
    failProjection = false;
    await page.evaluate(async ({ branchId, orgId }) => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      // @ts-ignore
      syncEngine.branchId = branchId;
      // @ts-ignore
      syncEngine.organizationId = orgId;
      // @ts-ignore
      await syncEngine.pullEvents();
    }, { branchId, orgId });

    const pullSeqAfterRecovery = await page.evaluate(async () => {
      // @ts-ignore
      const { getEventPullSeq } = await import('/src/lib/localDb.ts');
      return await getEventPullSeq();
    });

    // PROOF: Cursor successfully advanced through contiguous sequence to 3!
    expect(pullSeqAfterRecovery).toBe(3);

    // Verify local SQLite has the final recovered drug state
    const finalDrug = bridge.select<{ name: string; unit_price: number }>(
      'SELECT name, unit_price FROM drugs WHERE id = ?',
      [drugId]
    );
    expect(finalDrug[0].name).toBe('Test Drug Seq 3 Final');
    expect(finalDrug[0].unit_price).toBe(15.0);
  });

  test('4. Outbound synchronization continues to function cleanly', async ({ page }) => {
    await page.goto('/customers');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.getByRole('heading', { name: /Customers/i })).toBeVisible({ timeout: 15000 });

    const orgId = '11111111-1111-1111-1111-111111111111';
    const branchId = '22222222-2222-2222-2222-222222222222';
    const customerId = crypto.randomUUID();
    const firstName = `SyncClean${Date.now().toString().slice(-4)}`;
    const lastName = `Tester`;
    const phone = `+23324${Math.floor(1000000 + Math.random() * 9000000)}`;

    await page.evaluate(async ({ customerId, firstName, lastName, phone, orgId, branchId }) => {
      // @ts-ignore
      const { getDb } = await import('/src/lib/localDb.ts');
      await getDb();
      // @ts-ignore
      const { writeLocal } = await import('/src/lib/localWrite.ts');
      await writeLocal.customer({
        id: customerId,
        organization_id: orgId,
        customer_type: 'registered',
        first_name: firstName,
        last_name: lastName,
        phone,
        email: `${firstName.toLowerCase()}@example.com`,
        loyalty_tier: 'bronze',
        loyalty_points: 0,
      }, "create", branchId);
    }, { customerId, firstName, lastName, phone, orgId, branchId });

    // Verify outbox event was recorded locally
    const outboxEvents = bridge.select<{ event_id: string; status: string; event_type: string }>(
      "SELECT event_id, status, event_type FROM event_outbox WHERE event_type = 'customer_created' ORDER BY created_at DESC LIMIT 1"
    );
    expect(outboxEvents.length).toBe(1);

    // Trigger sync push
    await page.evaluate(async ({ branchId, orgId }) => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      // @ts-ignore
      syncEngine.branchId = branchId;
      // @ts-ignore
      syncEngine.organizationId = orgId;
      // @ts-ignore
      await syncEngine.pushEvents();
    }, { branchId, orgId });

    // Verify outbox status transitioned to accepted
    const updatedOutbox = bridge.select<{ status: string }>(
      'SELECT status FROM event_outbox WHERE event_id = ?',
      [outboxEvents[0].event_id]
    );
    expect(updatedOutbox[0].status).toBe('accepted');
  });
});
