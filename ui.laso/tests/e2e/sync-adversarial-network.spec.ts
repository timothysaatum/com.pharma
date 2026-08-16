import { test, expect } from '@playwright/test';
import { setupAuthenticatedState } from './helpers/auth-helper';
import { TauriSqliteBridge } from './helpers/tauri-bridge';
import { BackendDatabase } from './helpers/backend-db';

test.describe('Sync Adversarial & Network Failure E2E Tests', () => {
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

  test('1. Sync attempts while offline handle network errors gracefully and retain outbox', async ({ page }) => {
    await page.goto('/customers');
    await page.waitForLoadState('networkidle');

    // Register a customer locally
    const registerBtn = page.locator('button:has-text("Register Customer")').first();
    await registerBtn.click();
    await expect(page.locator('button[form="customer-form"]')).toBeVisible({ timeout: 5000 });

    // Cut network
    await page.context().setOffline(true);
    await page.waitForTimeout(300);

    const firstName = `Kofi`;
    const lastName = `Offline${Date.now().toString().slice(-4)}`;
    const custPhone = `+23324${Math.floor(1000000 + Math.random() * 9000000)}`;

    await page.selectOption('select[name="customer_type"]', 'registered');
    await page.fill('input[placeholder="First name"]', firstName);
    await page.fill('input[placeholder="Last name"]', lastName);
    const phoneInput = page.locator('input[placeholder*="20 000"], input[type="tel"], input[name="phone"]').first();
    await phoneInput.fill(custPhone);

    const submitBtn = page.locator('button[form="customer-form"]').first();
    await submitBtn.click();
    await page.waitForTimeout(1000);

    // Verify outbox has the event
    expect(bridge.getOutboxCount()).toBeGreaterThanOrEqual(1);

    // Attempt to manually trigger sync while offline
    const syncButton = page.locator('button[title*="Sync"], button:has(.lucide-refresh-cw)').first();
    await syncButton.click();
    await page.waitForTimeout(1000);

    // Outbox should STILL retain the pending event
    expect(bridge.getOutboxCount()).toBeGreaterThanOrEqual(1);

    // Restore network
    await page.context().setOffline(false);
    await page.waitForTimeout(1500);

    // Sync again
    await syncButton.click();

    // Verify outbox is completely drained
    await expect.poll(() => bridge.getOutboxCount(), { timeout: 15000 }).toBe(0);

    // Verify customer arrived in backend PostgreSQL
    await expect.poll(async () => {
      const rows = await backendDb.query(
        'SELECT id, first_name, last_name FROM customers WHERE organization_id = $1 AND first_name = $2 AND last_name = $3',
        [orgId, firstName, lastName]
      );
      return rows.length;
    }, { timeout: 15000 }).toBe(1);
  });

  test('2. Server 500 errors during push are caught, flagged as failed, and retried on next cycle', async ({ page }) => {
    await page.goto('/customers');
    await page.waitForLoadState('networkidle');

    const custId = '55555555-5555-5555-5555-' + Math.random().toString(16).substring(2, 14).padEnd(12, '0');
    const firstName = `Akua`;
    const lastName = `Retry500${Date.now().toString().slice(-4)}`;
    const custPhone = `+23324${Math.floor(1000000 + Math.random() * 9000000)}`;

    // Write a pending customer event locally to outbox
    await page.evaluate(async ({ custId, orgId, branchId, firstName, lastName, custPhone }) => {
      // @ts-ignore
      const { writeLocal } = await import('/src/lib/localWrite.ts');
      const now = new Date().toISOString();
      await writeLocal.customer({
        id: custId,
        organization_id: orgId,
        customer_type: 'registered',
        first_name: firstName,
        last_name: lastName,
        phone: custPhone,
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
    }, { custId, orgId, branchId, firstName, lastName, custPhone });

    // Verify outbox has the pending event
    expect(bridge.getOutboxCount()).toBeGreaterThanOrEqual(1);

    // Intercept push endpoint and simulate 500 internal server error
    let failureInjected = true;
    await page.route('**/api/v1/sync/events', async (route) => {
      if (failureInjected && route.request().method() === 'POST') {
        await route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Simulated backend database outage' }),
        });
      } else {
        await route.continue();
      }
    });

    // Trigger sync
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      try {
        await syncEngine.sync();
      } catch {}
    });

    // Event should still be in outbox (marked failed or pending for retry)
    const outboxEvents = bridge.select<{ status: string; error_code: string }>(
      'SELECT status, error_code FROM event_outbox'
    );
    expect(outboxEvents.length).toBeGreaterThanOrEqual(1);
    expect(['pending', 'failed']).toContain(outboxEvents[0].status);

    // Now unblock the route (server recovered)
    failureInjected = false;
    await page.unroute('**/api/v1/sync/events');

    // Trigger next sync cycle
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });

    // Verify outbox drains to 0
    await expect.poll(() => bridge.getOutboxCount(), { timeout: 15000 }).toBe(0);

    // Verify customer reached PostgreSQL
    await expect.poll(async () => {
      const rows = await backendDb.query(
        'SELECT id, first_name, last_name FROM customers WHERE organization_id = $1 AND first_name = $2 AND last_name = $3',
        [orgId, firstName, lastName]
      );
      return rows.length;
    }, { timeout: 15000 }).toBe(1);
  });

  test('3. Network latency during sync displays active sync state and finishes cleanly', async ({ page }) => {
    await page.goto('/customers');
    await page.waitForLoadState('networkidle');

    // Add artificial latency to sync pull requests
    await page.route('**/api/v1/sync/events?*', async (route) => {
      await new Promise((res) => setTimeout(res, 1200));
      await route.continue();
    });

    const syncButton = page.locator('button[title*="Sync"], button:has(.lucide-refresh-cw)').first();
    await syncButton.click();

    // Verify syncing indicator / spinning icon appears
    const spinningIcon = page.locator('.animate-spin, [class*="animate-spin"]').first();
    await expect(spinningIcon).toBeVisible({ timeout: 5000 });

    // Wait for sync to complete
    await expect(page.getByText(/Just now|0 pending/i).first()).toBeVisible({ timeout: 15000 });
  });
});
