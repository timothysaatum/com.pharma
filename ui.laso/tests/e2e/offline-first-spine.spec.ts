import { test, expect } from '@playwright/test';
import { TauriSqliteBridge } from './helpers/tauri-bridge';
import { setupAuthenticatedState } from './helpers/auth-helper';
import { BackendDatabase } from './helpers/backend-db';

test.describe('Offline-First Architecture & Event Spine E2E Tests', () => {
  let bridge: TauriSqliteBridge;
  let backendDb: BackendDatabase;

  const orgId = '11111111-1111-1111-1111-111111111111';
  const branchId = '22222222-2222-2222-2222-222222222222';
  const categoryId = '55555555-5555-5555-5555-555555555555';
  const drugId = '66666666-6666-6666-6666-666666666666';
  const customerId = '88888888-8888-8888-8888-888888888888';
  const contractId = '99999999-9999-9999-9999-999999999999';
  const prescriptionId = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

  test.beforeAll(async () => {
    backendDb = new BackendDatabase();

    // Seed comprehensive event stream in PostgreSQL event_log
    const now = new Date().toISOString();

    // 1. Category
    await backendDb.insertServerEvent({
      org_id: orgId,
      branch_id: branchId,
      aggregate_id: categoryId,
      aggregate_type: 'drug_category',
      event_type: 'drug_category_created',
      payload: {
        id: categoryId,
        organization_id: orgId,
        name: 'Analgesics & Pain Relief',
        description: 'Pain relief and antipyretic drugs',
        is_active: true,
        created_at: now,
        updated_at: now,
      },
    });

    // 2. Drug
    await backendDb.insertServerEvent({
      org_id: orgId,
      branch_id: branchId,
      aggregate_id: drugId,
      aggregate_type: 'drug',
      event_type: 'drug_created',
      payload: {
        id: drugId,
        organization_id: orgId,
        category_id: categoryId,
        name: 'Paracetamol 500mg Tablets',
        generic_name: 'Acetaminophen',
        brand_name: 'Panadol',
        sku: 'PARA-500',
        barcode: '8901234567890',
        drug_type: 'otc',
        dosage_form: 'tablet',
        strength: '500mg',
        unit_price: 10.0,
        cost_price: 5.0,
        reorder_level: 20,
        requires_prescription: false,
        is_active: true,
        created_at: now,
        updated_at: now,
      },
    });

    // 3. Branch Inventory
    await backendDb.insertServerEvent({
      org_id: orgId,
      branch_id: branchId,
      aggregate_id: drugId,
      aggregate_type: 'branch_inventory',
      event_type: 'branch_inventory_updated',
      payload: {
        branch_id: branchId,
        drug_id: drugId,
        quantity: 250,
        reserved_quantity: 0,
        location: 'Aisle 2, Shelf A',
        selling_price: 10.0,
      },
    });

    // 4. Customer
    await backendDb.insertServerEvent({
      org_id: orgId,
      branch_id: branchId,
      aggregate_id: customerId,
      aggregate_type: 'customer',
      event_type: 'customer_created',
      payload: {
        id: customerId,
        organization_id: orgId,
        first_name: 'Kwame',
        last_name: 'Nkrumah',
        name: 'Kwame Nkrumah',
        phone: '+233241234567',
        email: 'kwame@ghana.gov',
        customer_type: 'registered',
        loyalty_points: 150,
        loyalty_tier: 'gold',
        is_active: true,
        created_at: now,
        updated_at: now,
      },
    });

    // 5. Price Contract
    await backendDb.insertServerEvent({
      org_id: orgId,
      branch_id: branchId,
      aggregate_id: contractId,
      aggregate_type: 'price_contract',
      event_type: 'price_contract_created',
      payload: {
        id: contractId,
        organization_id: orgId,
        contract_name: 'National Health Insurance Discount',
        contract_code: 'NHIS-2026',
        contract_type: 'insurance',
        discount_percentage: 15.0,
        status: 'active',
        is_active: true,
        applies_to_all_branches: true,
        created_at: now,
        updated_at: now,
      },
    });

    // 6. Prescription
    await backendDb.insertServerEvent({
      org_id: orgId,
      branch_id: branchId,
      aggregate_id: prescriptionId,
      aggregate_type: 'prescription',
      event_type: 'prescription_created',
      payload: {
        id: prescriptionId,
        organization_id: orgId,
        branch_id: branchId,
        customer_id: customerId,
        patient_name: 'Kwame Nkrumah',
        prescriber_name: 'Dr. Mensah',
        prescription_number: 'RX-2026-999',
        status: 'active',
        issue_date: '2026-08-16',
        expiry_date: '2026-12-31',
        diagnosis: 'Mild fever',
        created_at: now,
        updated_at: now,
      },
    });

    await backendDb.insertServerEvent({
      org_id: orgId,
      branch_id: branchId,
      aggregate_id: prescriptionId,
      aggregate_type: 'prescription',
      event_type: 'prescription_updated',
      payload: {
        id: prescriptionId,
        status: 'active',
        issue_date: '2026-08-16',
        expiry_date: '2026-12-31',
        updated_at: now,
      },
    });
  });

  test.afterAll(async () => {
    await backendDb.close();
  });

  test.beforeEach(async ({ page }) => {
    test.setTimeout(90000);
    page.on('console', msg => console.log(`[BROWSER ${msg.type()}]:`, msg.text()));
    page.on('pageerror', err => console.error('[BROWSER ERROR]:', err));
    bridge = new TauriSqliteBridge();
    await bridge.attachToPage(page);
    await setupAuthenticatedState(page, bridge);
  });

  test('Complete Offline-First lifecycle: Sync -> Disconnect -> Navigate 7 screens -> Mutate Outbox -> Reconnect -> Reconcile', async ({ page }) => {
    // ══════════════════════════════════════════════════════════════════════
    // PHASE 1: Online Initial Sync
    // ══════════════════════════════════════════════════════════════════════
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');

    // Trigger full sync from backend into local SQLite
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });

    // Wait for sync to complete
    await expect(page.getByText(/Just now|\d+m ago|0 pending/i).first()).toBeVisible({ timeout: 20000 });

    // Verify local SQLite has populated records from the event log
    const drugsInDb = bridge.getTableRows('drugs');
    console.log(`[E2E] Synced drugs in SQLite: ${drugsInDb.length}`);
    expect(drugsInDb.length).toBeGreaterThan(0);

    const inventoryInDb = bridge.getTableRows('branch_inventory');
    console.log(`[E2E] Synced branch inventory in SQLite: ${inventoryInDb.length}`);
    expect(inventoryInDb.length).toBeGreaterThan(0);

    const customersInDb = bridge.getTableRows('customers');
    console.log(`[E2E] Synced customers in SQLite: ${customersInDb.length}`);
    expect(customersInDb.length).toBeGreaterThan(0);

    const rxInDb = bridge.getTableRows('prescriptions');
    console.log(`[E2E] Synced prescriptions in SQLite: ${JSON.stringify(rxInDb)}`);

    // ══════════════════════════════════════════════════════════════════════
    // PHASE 2: Disconnect Network (Simulate Backend Offline / Connection Refused)
    // ══════════════════════════════════════════════════════════════════════
    console.log('[E2E] Simulating network drop / backend connection refused...');
    let networkBlocked = true;

    await page.route('**/api/v1/**', async (route) => {
      if (networkBlocked) {
        await route.abort('failed');
      } else {
        await route.continue();
      }
    });

    // Explicitly mark backend offline on the client side
    await page.evaluate(async () => {
      // @ts-ignore
      const { markBackendOffline } = await import('/src/api/client.ts');
      markBackendOffline();
    });

    // ══════════════════════════════════════════════════════════════════════
    // PHASE 3: Navigate all core screens while fully OFFLINE
    // ══════════════════════════════════════════════════════════════════════

    // 1. POS Drug Search (Offline from SQLite)
    console.log('[E2E] Testing POS search while offline...');
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');

    const searchInput = page.locator('input[placeholder*="Search"]').first();
    await searchInput.waitFor({ state: 'visible', timeout: 15000 });
    await searchInput.fill('Paracetamol');
    await expect(page.getByText(/Paracetamol/i).first()).toBeVisible({ timeout: 15000 });

    // 2. Sales History (Offline from SQLite)
    console.log('[E2E] Testing Sales History while offline...');
    await page.goto('/sales');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.getByText(/Sales History/i).first()).toBeVisible({ timeout: 10000 });

    // 3. Drugs Catalogue (Offline from SQLite)
    console.log('[E2E] Testing Drugs Catalogue while offline...');
    await page.goto('/admin/drugs');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.getByText(/Drug Catalogue|Drugs/i).first()).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(/Paracetamol/i).first()).toBeVisible({ timeout: 10000 });

    // 4. Branch Inventory (Offline from SQLite)
    console.log('[E2E] Testing Branch Inventory while offline...');
    await page.goto('/admin/inventory');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.getByText(/Inventory/i).first()).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(/Paracetamol/i).first()).toBeVisible({ timeout: 10000 });

    // 5. Customers List (Offline from SQLite)
    console.log('[E2E] Testing Customers while offline...');
    await page.goto('/customers');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.getByText(/Customers/i).first()).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(/Kwame Nkrumah/i).first()).toBeVisible({ timeout: 10000 });

    // 6. Price Contracts (Offline from SQLite)
    console.log('[E2E] Testing Contracts while offline...');
    await page.goto('/admin/contracts');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.getByText(/Contracts|Price Contracts/i).first()).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(/National Health Insurance/i).first()).toBeVisible({ timeout: 10000 });

    // 7. Prescriptions (Offline from SQLite)
    console.log('[E2E] Testing Prescriptions while offline...');
    await page.goto('/prescriptions');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.getByText(/Prescriptions/i).first()).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(/Kwame Nkrumah|RX-2026-999/i).first()).toBeVisible({ timeout: 10000 });

    // ══════════════════════════════════════════════════════════════════════
    // PHASE 4: Offline Mutation (Create Stock Adjustment into event_outbox)
    // ══════════════════════════════════════════════════════════════════════
    console.log('[E2E] Recording offline stock adjustment mutation...');
    const outboxBefore = bridge.getTableRows('event_outbox');
    const outboxCountBefore = outboxBefore.length;

    await page.evaluate(async ({ drugId, branchId, orgId }) => {
      // @ts-ignore
      const { writeLocal } = await import('/src/lib/localWrite.ts');
      await writeLocal.stockAdjustment({
        id: crypto.randomUUID(),
        branch_id: branchId,
        drug_id: drugId,
        adjustment_type: 'correction',
        quantity_change: 5,
        reason: 'E2E Offline count correction',
        adjusted_by: 'user-admin',
        organization_id: orgId,
      });
    }, { drugId, branchId, orgId });

    // Verify outbox recorded the event envelope
    const outboxAfter = bridge.getTableRows('event_outbox');
    expect(outboxAfter.length).toBe(outboxCountBefore + 1);
    const lastOutboxEvent = outboxAfter[outboxAfter.length - 1];
    expect(lastOutboxEvent.event_type).toBe('stock_adjusted');
    expect(lastOutboxEvent.status).toBe('pending');

    // ══════════════════════════════════════════════════════════════════════
    // PHASE 5: Reconnect & Reconcile
    // ══════════════════════════════════════════════════════════════════════
    console.log('[E2E] Reconnecting network and pushing outbox events...');
    networkBlocked = false;
    await page.unroute('**/api/v1/**');

    // Mark backend back online and run sync
    await page.evaluate(async () => {
      // @ts-ignore
      const { markBackendOnline } = await import('/src/api/client.ts');
      markBackendOnline();

      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });

    // Verify event outbox is pushed and cleared
    const outboxFinal = bridge.getTableRows('event_outbox');
    const pendingEvents = outboxFinal.filter((e: any) => e.status === 'pending');
    expect(pendingEvents.length).toBe(0);
    console.log('[E2E] All offline mutations successfully pushed to server and reconciled!');
  });
});
