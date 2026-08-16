import { test, expect } from '@playwright/test';
import { TauriSqliteBridge } from './helpers/tauri-bridge';
import { setupAuthenticatedState } from './helpers/auth-helper';
import { BackendDatabase } from './helpers/backend-db';

test.describe('Sync Data Consistency & Multi-Directional E2E Tests', () => {
  let bridge: TauriSqliteBridge;
  let backendDb: BackendDatabase;
  const orgId = '11111111-1111-1111-1111-111111111111';
  const branchId = '22222222-2222-2222-2222-222222222222';
  const drugId = '33333333-3333-3333-3333-333333333331'; // Amoxicillin

  test.beforeAll(async () => {
    backendDb = new BackendDatabase();
  });

  test.afterAll(async () => {
    await backendDb.close();
  });

  test.beforeEach(async ({ page }) => {
    page.on('console', msg => console.log(`[BROWSER ${msg.type()}]:`, msg.text()));
    page.on('pageerror', err => console.error('[BROWSER ERROR]:', err));
    bridge = new TauriSqliteBridge();
    await bridge.attachToPage(page);
    await setupAuthenticatedState(page, bridge);
  });

  test('1. Server-side new batch is pulled to client SQLite and reflected in POS search', async ({ page }) => {
    await page.goto('/pos');
    await page.waitForLoadState('networkidle');
    await expect(page.getByText(/Just now|0 pending/i).first()).toBeVisible({ timeout: 15000 });

    const newBatchId = crypto.randomUUID();
    const newBatchNumber = `BATCH-E2E-${Date.now()}`;

    // Insert server-side event
    await backendDb.insertServerEvent({
      org_id: orgId,
      branch_id: branchId,
      aggregate_id: newBatchId,
      aggregate_type: 'drug_batch',
      event_type: 'drug_batch_created',
      payload: {
        id: newBatchId,
        drug_id: drugId,
        branch_id: branchId,
        org_id: orgId,
        batch_number: newBatchNumber,
        quantity: 50,
        remaining_quantity: 50,
        cost_price: 8.5,
        selling_price: 15.0,
        expiry_date: '2027-12-31',
        received_date: new Date().toISOString().split('T')[0],
      },
    });

    // Trigger sync
    const syncButton = page.locator('button[title*="Sync"], button:has(.lucide-refresh-cw)').first();
    await syncButton.click();
    await page.waitForTimeout(1500);
    await expect(page.getByText(/Just now|0 pending/i).first()).toBeVisible({ timeout: 15000 });

    // Verify batch exists in local SQLite
    const localBatches = bridge.select<{ id: string; batch_number: string }>(
      'SELECT id, batch_number FROM drug_batches WHERE id = ?',
      [newBatchId]
    );
    expect(localBatches.length).toBe(1);
    expect(localBatches[0].batch_number).toBe(newBatchNumber);
  });

  test('2. Server-side customer creation is projected to SQLite and Customers UI', async ({ page }) => {
    await page.goto('/customers');
    await page.waitForLoadState('networkidle');
    await expect(page.getByText(/Just now|0 pending/i).first()).toBeVisible({ timeout: 15000 });

    const custId = crypto.randomUUID();
    const firstName = `Kwame`;
    const lastName = `Mensah${Date.now().toString().slice(-4)}`;
    const custPhone = `+23324${Math.floor(1000000 + Math.random() * 9000000)}`;

    // Insert server event: customer_created
    await backendDb.insertServerEvent({
      org_id: orgId,
      aggregate_id: custId,
      aggregate_type: 'customer',
      event_type: 'customer_created',
      payload: {
        id: custId,
        organization_id: orgId,
        first_name: firstName,
        last_name: lastName,
        name: `${firstName} ${lastName}`,
        phone: custPhone,
        customer_type: 'registered',
        is_active: true,
      },
    });

    // Trigger sync
    const syncButton = page.locator('button[title*="Sync"], button:has(.lucide-refresh-cw)').first();
    await syncButton.click();
    await page.waitForTimeout(1500);
    await expect(page.getByText(/Just now|0 pending/i).first()).toBeVisible({ timeout: 15000 });

    // Reload customers list
    await page.reload();
    await page.waitForLoadState('networkidle');

    // Verify customer is in SQLite
    const localCust = bridge.select<{ first_name: string; phone: string }>(
      'SELECT first_name, phone FROM customers WHERE id = ?',
      [custId]
    );
    expect(localCust.length).toBe(1);
    expect(localCust[0].first_name).toBe(firstName);

    // Verify customer name is visible on the page
    await expect(page.getByText(firstName).first()).toBeVisible({ timeout: 10000 });
  });

  test('3. Offline client customer registration appends outbox and pushes to PostgreSQL on sync', async ({ page }) => {
    await page.goto('/customers');
    await page.waitForLoadState('networkidle');
    await expect(page.getByText(/Just now|0 pending/i).first()).toBeVisible({ timeout: 15000 });

    // Open modal while page bundle is loaded
    const registerBtn = page.locator('button:has-text("Register Customer")').first();
    await expect(registerBtn).toBeVisible({ timeout: 10000 });
    await registerBtn.click();

    // Verify modal is open
    await expect(page.locator('button[form="customer-form"]')).toBeVisible({ timeout: 5000 });

    // Go offline for the backend API calls
    await page.context().setOffline(true);
    await page.waitForTimeout(300);

    const firstName = `Ama`;
    const lastName = `Serwaa${Date.now().toString().slice(-4)}`;
    const custPhone = `+23320${Math.floor(1000000 + Math.random() * 9000000)}`;

    // Select registered customer type
    await page.selectOption('select[name="customer_type"]', 'registered');

    // Fill personal info
    await page.fill('input[placeholder="First name"]', firstName);
    await page.fill('input[placeholder="Last name"]', lastName);
    const phoneInput = page.locator('input[placeholder*="20 000"], input[type="tel"], input[name="phone"]').first();
    await phoneInput.fill(custPhone);

    // Submit form offline
    const submitBtn = page.locator('button[form="customer-form"]').first();
    await submitBtn.click();
    await page.waitForTimeout(1000);

    // Verify local SQLite has 1 outbox event
    const outboxCountBefore = bridge.getOutboxCount();
    expect(outboxCountBefore).toBeGreaterThanOrEqual(1);

    // Go back online
    await page.context().setOffline(false);
    await page.waitForTimeout(1500);

    // Click sync to flush the outbox
    const syncButton = page.locator('button[title*="Sync"], button:has(.lucide-refresh-cw)').first();
    await syncButton.click();
    
    // Wait for sync to settle and outbox to drain
    await expect.poll(() => bridge.getOutboxCount(), { timeout: 15000 }).toBe(0);

    // Verify customer record in server PostgreSQL customers table
    await expect.poll(async () => {
      const rows = await backendDb.query(
        'SELECT id, first_name, last_name, phone FROM customers WHERE organization_id = $1 AND first_name = $2 AND last_name = $3',
        [orgId, firstName, lastName]
      );
      return rows.length;
    }, { timeout: 15000 }).toBe(1);
  });
});
