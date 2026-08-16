import { test, expect } from '@playwright/test';
import { TauriSqliteBridge } from './helpers/tauri-bridge';
import { setupAuthenticatedState } from './helpers/auth-helper';
import { BackendDatabase } from './helpers/backend-db';

test.describe('POS Sale Checkout End-to-End Tests', () => {
  let bridge: TauriSqliteBridge;
  let backendDb: BackendDatabase;
  let orgId: string;
  let branchId: string;
  let userId: string;

  test.beforeEach(async ({ page }) => {
    test.setTimeout(60000);

    page.on('console', msg => console.log(`[BROWSER ${msg.type()}]:`, msg.text()));
    page.on('pageerror', err => console.error('[BROWSER ERROR]:', err));

    backendDb = new BackendDatabase();
    bridge = new TauriSqliteBridge();
    await bridge.attachToPage(page);

    const auth = await setupAuthenticatedState(page, bridge);
    orgId = auth.orgId;
    branchId = auth.branchId;
    userId = auth.user.id;

    // Seed default standard price contract in PostgreSQL
    await backendDb.seedDefaultPriceContract(orgId);
  });

  test.afterEach(async () => {
    await backendDb.close();
  });

  test('1. Full online POS checkout completes sale, shows receipt modal, and deducts inventory in PostgreSQL', async ({ page }) => {
    const drugId = crypto.randomUUID();
    const drugName = `Paracetamol-${Date.now().toString().slice(-4)}`;
    const unitPrice = 20.00;
    const initialQty = 50;

    // Seed drug, branch inventory, and batch in PostgreSQL
    await backendDb.seedDrugAndStock({
      org_id: orgId,
      branch_id: branchId,
      drug_id: drugId,
      name: drugName,
      unit_price: unitPrice,
      quantity: initialQty,
    });

    // Navigate to POS page
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');

    // Search for the seeded drug
    const searchInput = page.locator('input[placeholder*="Search drug"]').first();
    await expect(searchInput).toBeVisible({ timeout: 15000 });
    await searchInput.fill(drugName);

    // Click the drug item to add to Cart
    const drugCard = page.locator('button', { hasText: drugName }).first();
    await expect(drugCard).toBeVisible({ timeout: 20000 });
    await drugCard.click();

    // Verify cart subtotal and total on the right
    await expect(page.locator('text=Subtotal (1 item)')).toBeVisible({ timeout: 5000 });
    await expect(page.getByText(`₵${unitPrice.toFixed(2)}`).first()).toBeVisible();

    // Amount tendered input (uniquely identified by step="0.01")
    const amountTenderedInput = page.locator('input[step="0.01"]').first();
    await amountTenderedInput.fill('50.00');

    // Verify change calculation (₵30.00)
    await expect(page.getByText('Change due')).toBeVisible();
    await expect(page.getByText('₵30.00')).toBeVisible();

    // Click Complete Sale button
    const checkoutButton = page.locator('button:has-text("Complete Sale")').first();
    await expect(checkoutButton).toBeEnabled();
    await checkoutButton.click();

    // Verify Sale Success Modal appears
    await expect(page.getByRole('heading', { name: /Sale Complete/i })).toBeVisible({ timeout: 35000 });
    await expect(page.getByText('Total charged')).toBeVisible();
    await expect(page.getByText('₵20.00').first()).toBeVisible();
    await expect(page.locator('button:has-text("New Sale")').first()).toBeVisible();

    // Dismiss modal via New Sale button
    await page.locator('button:has-text("New Sale")').first().click();

    // Verify cart is cleared back to empty state
    await expect(page.getByText('Cart is empty')).toBeVisible();

    // Verify PostgreSQL database state:
    // 1. Sales row created
    const sales = await backendDb.getSales(orgId);
    expect(sales.length).toBeGreaterThanOrEqual(1);
    const latestSale = sales[0];
    expect(Number(latestSale.total_amount)).toBe(20.00);
    expect(latestSale.status).toBe('completed');
    expect(latestSale.payment_method).toBe('cash');

    // 2. Batch and branch inventory decremented
    const batches = await backendDb.getDrugBatches(branchId);
    const updatedBatch = batches.find(b => b.drug_id === drugId);
    expect(updatedBatch).toBeDefined();
    expect(updatedBatch.remaining_quantity).toBe(initialQty - 1);

    const inv = await backendDb.getBranchInventory(branchId);
    const updatedInv = inv.find(i => i.drug_id === drugId);
    expect(updatedInv).toBeDefined();
    expect(updatedInv.quantity).toBe(initialQty - 1);
  });

  test('2. Offline POS sale checkout completes locally, stores outbox event, and syncs to PostgreSQL on reconnect', async ({ page }) => {
    const drugId = crypto.randomUUID();
    const drugName = `Amoxicillin-${Date.now().toString().slice(-4)}`;
    const unitPrice = 15.00;
    const initialQty = 30;

    // Seed drug and inventory in PG
    await backendDb.seedDrugAndStock({
      org_id: orgId,
      branch_id: branchId,
      drug_id: drugId,
      name: drugName,
      unit_price: unitPrice,
      quantity: initialQty,
    });

    // Navigate to POS first to initialize SQLite schema and pull initial price contracts
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.locator('input[placeholder*="Search drug"]').first()).toBeVisible({ timeout: 20000 });

    // Seed drug, branch_inventory, drug_batches, and price_contracts in local SQLite
    bridge.execute(
      `INSERT INTO drugs (id, organization_id, name, drug_type, unit_price, requires_prescription, is_active, is_deleted, created_at, updated_at)
       VALUES (?, ?, ?, 'otc', ?, 0, 1, 0, datetime('now'), datetime('now'))
       ON CONFLICT (id) DO UPDATE SET unit_price = ?`,
      [drugId, orgId, drugName, unitPrice, unitPrice]
    );
    bridge.execute(
      `INSERT INTO branch_inventory (id, branch_id, drug_id, quantity, sellable_quantity, selling_price, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
       ON CONFLICT (id) DO NOTHING`,
      [crypto.randomUUID(), branchId, drugId, initialQty, initialQty, unitPrice]
    );
    bridge.execute(
      `INSERT INTO drug_batches (id, branch_id, drug_id, batch_number, quantity, remaining_quantity, expiry_date, selling_price, cost_price, created_at, updated_at)
       VALUES (?, ?, ?, 'BATCH-OFFLINE-01', ?, ?, '2028-12-31', ?, 5.0, datetime('now'), datetime('now'))
       ON CONFLICT (id) DO NOTHING`,
      [crypto.randomUUID(), branchId, drugId, initialQty, initialQty, unitPrice]
    );
    bridge.execute(
      `INSERT INTO price_contracts (
        id, organization_id, contract_code, contract_name, contract_type,
        is_default_contract, discount_type, discount_percentage,
        applies_to_prescription_only, applies_to_otc,
        applies_to_all_branches, applicable_branch_ids, effective_from,
        status, is_active, is_deleted, created_at, updated_at
      ) VALUES (
        '33333333-3333-3333-3333-333333333333', ?, 'STD-001', 'Standard Retail', 'standard',
        1, 'percentage', 0.0,
        0, 1,
        1, '[]', '2020-01-01',
        'active', 1, 0, datetime('now'), datetime('now')
      ) ON CONFLICT (id) DO UPDATE SET is_active = 1, effective_from = '2020-01-01'`,
      [orgId]
    );

    // Intercept backend endpoints to simulate network disconnect
    let offline = true;
    await page.route('**/api/v1/**', async (route) => {
      if (offline) {
        await route.abort('failed');
      } else {
        await route.continue();
      }
    });

    // Notify app of offline state
    await page.evaluate(() => window.dispatchEvent(new Event('offline')));

    // Re-search in POS
    const searchInput = page.locator('input[placeholder*="Search drug"]').first();
    await expect(searchInput).toBeVisible({ timeout: 10000 });
    await searchInput.fill(drugName);

    const drugCard = page.locator('button', { hasText: drugName }).first();
    await expect(drugCard).toBeVisible({ timeout: 15000 });
    await drugCard.click();

    // Verify cart has the item
    await expect(page.locator('text=Subtotal (1 item)')).toBeVisible({ timeout: 5000 });

    // Complete sale offline
    const checkoutButton = page.locator('button:has-text("Complete Sale")').first();
    await expect(checkoutButton).toBeEnabled({ timeout: 10000 });
    await checkoutButton.click();

    // Verify success confirmation modal appears
    await expect(page.getByRole('heading', { name: /Sale Complete/i })).toBeVisible({ timeout: 20000 });
    await page.locator('button:has-text("New Sale")').first().click();

    // Verify local SQLite has sale record and pending outbox event
    const localSales = bridge.select<{ id: string; total_amount: number; sync_status: string }>(
      'SELECT id, total_amount, sync_status FROM sales'
    );
    expect(localSales.length).toBeGreaterThanOrEqual(1);

    const outboxEvents = bridge.select<{ event_type: string; status: string; hash_self: string }>(
      "SELECT event_type, status, hash_self FROM event_outbox WHERE event_type = 'sale_created'"
    );
    expect(outboxEvents.length).toBeGreaterThanOrEqual(1);
    expect(['pending', 'failed']).toContain(outboxEvents[0].status);
    expect(outboxEvents[0].hash_self).toHaveLength(64);

    // Reconnect online: unroute network intercepts and dispatch online event
    offline = false;
    await page.unroute('**/api/v1/**');
    await page.evaluate(() => window.dispatchEvent(new Event('online')));

    // Trigger sync cycle to push offline sale to server
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });

    // Verify outbox event is accepted and drains
    await expect.poll(() => bridge.getOutboxCount(), { timeout: 15000 }).toBe(0);

    // Verify backend PostgreSQL now has the sale and decremented inventory
    await expect.poll(async () => {
      const sales = await backendDb.getSales(orgId);
      return sales.filter(s => Number(s.total_amount) === 15.00).length;
    }, { timeout: 15000 }).toBeGreaterThanOrEqual(1);

    const batches = await backendDb.getDrugBatches(branchId);
    const updatedBatch = batches.find(b => b.drug_id === drugId);
    expect(updatedBatch).toBeDefined();
    expect(updatedBatch.remaining_quantity).toBe(initialQty - 1);
  });

  test('3. Cashier validation blocks checkout when payment amount is less than total', async ({ page }) => {
    const drugId = crypto.randomUUID();
    const drugName = `Ibuprofen-${Date.now().toString().slice(-4)}`;
    const unitPrice = 25.00;

    await backendDb.seedDrugAndStock({
      org_id: orgId,
      branch_id: branchId,
      drug_id: drugId,
      name: drugName,
      unit_price: unitPrice,
      quantity: 40,
    });

    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');

    // Search and add drug to cart
    const searchInput = page.locator('input[placeholder*="Search drug"]').first();
    await expect(searchInput).toBeVisible({ timeout: 15000 });
    await searchInput.fill(drugName);

    const drugCard = page.locator('button', { hasText: drugName }).first();
    await expect(drugCard).toBeVisible({ timeout: 20000 });
    await drugCard.click();

    // Tender short payment (₵10.00 on a ₵25.00 total)
    const amountTenderedInput = page.locator('input[step="0.01"]').first();
    await amountTenderedInput.fill('10.00');

    // Verify validation error is displayed and checkout is blocked
    const checkoutButton = page.locator('button:has-text("Complete Sale")').first();
    await expect(checkoutButton).toBeDisabled();
    await expect(page.getByText(/Amount paid .* is less than total/i).first()).toBeVisible();

    // Tender sufficient payment (₵25.00)
    await amountTenderedInput.fill('25.00');
    await expect(checkoutButton).toBeEnabled();
    await expect(page.getByText(/Amount paid .* is less than total/i)).toHaveCount(0);
  });
});
