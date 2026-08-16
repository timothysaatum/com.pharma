import { test, expect } from '@playwright/test';
import { TauriSqliteBridge } from './helpers/tauri-bridge';
import { setupAuthenticatedState } from './helpers/auth-helper';
import { BackendDatabase } from './helpers/backend-db';

test.describe('All Pages Offline-First & Online Navigation E2E Audit', () => {
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
    const now = new Date().toISOString();

    // Seed events
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
  });

  test.afterAll(async () => {
    await backendDb.close();
  });

  test.beforeEach(async ({ page }) => {
    test.setTimeout(120000);
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        console.error(`[PAGE ERROR]: ${msg.text()}`);
      }
    });
    page.on('pageerror', (err) => {
      console.error(`[UNCAUGHT EXCEPTION]:`, err);
    });
    bridge = new TauriSqliteBridge();
    await bridge.attachToPage(page);
    await setupAuthenticatedState(page, bridge);
  });

  test('All pages render and function in both ONLINE and OFFLINE states without crashes', async ({ page }) => {
    // ══════════════════════════════════════════════════════════════════════
    // PHASE 1: Online verification across all major routes
    // ══════════════════════════════════════════════════════════════════════
    console.log('[E2E-AUDIT] Starting ONLINE route checks...');

    // Sync data first
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });

    const routesToCheck = [
      { path: '/pos', heading: /Point of Sale|Search/i },
      { path: '/sales', heading: /Sales History/i },
      { path: '/customers', heading: /Customers/i },
      { path: '/prescriptions', heading: /Prescriptions/i },
      { path: '/admin/drugs', heading: /Drug|Drugs/i },
      { path: '/admin/inventory', heading: /Inventory/i },
      { path: '/admin/purchases', heading: /Purchase|Orders/i },
      { path: '/admin/contracts', heading: /Contracts/i },
      { path: '/reports', heading: /Reports/i },
      { path: '/users', heading: /Users/i },
      { path: '/settings', heading: /Settings|Organisation/i },
      { path: '/settings/branches', heading: /Branches/i },
      { path: '/settings/roles', heading: /Roles/i },
      { path: '/audit-logs', heading: /Audit/i },
      { path: '/conflicts', heading: /Conflicts/i },
    ];

    for (const route of routesToCheck) {
      console.log(`[E2E-AUDIT] ONLINE: Checking route ${route.path}...`);
      await page.goto(route.path);
      await page.waitForLoadState('domcontentloaded');
      await expect(page.getByText(route.heading).first()).toBeVisible({ timeout: 15000 });
      // Ensure no blank page or crash occurred
      const bodyText = await page.innerText('body');
      expect(bodyText.length).toBeGreaterThan(50);
    }

    // ══════════════════════════════════════════════════════════════════════
    // PHASE 2: Disconnect backend / simulate complete offline
    // ══════════════════════════════════════════════════════════════════════
    console.log('[E2E-AUDIT] Disconnecting backend to simulate OFFLINE mode...');
    await page.route('**/api/v1/**', async (route) => {
      await route.abort('failed');
    });

    await page.evaluate(async () => {
      // @ts-ignore
      const { markBackendOffline } = await import('/src/api/client.ts');
      markBackendOffline();
    });

    // ══════════════════════════════════════════════════════════════════════
    // PHASE 3: Offline verification across all major routes
    // ══════════════════════════════════════════════════════════════════════
    console.log('[E2E-AUDIT] Starting OFFLINE route checks...');

    for (const route of routesToCheck) {
      console.log(`[E2E-AUDIT] OFFLINE: Checking route ${route.path}...`);
      await page.goto(route.path);
      await page.waitForLoadState('domcontentloaded');
      await expect(page.getByText(route.heading).first()).toBeVisible({ timeout: 15000 });

      // Verify specific data items render offline
      if (route.path === '/admin/drugs' || route.path === '/admin/inventory' || route.path === '/pos') {
        await expect(page.getByText(/Paracetamol/i).first()).toBeVisible({ timeout: 15000 });
      }
      if (route.path === '/customers') {
        await expect(page.getByText(/Kwame Nkrumah/i).first()).toBeVisible({ timeout: 15000 });
      }
      if (route.path === '/admin/contracts') {
        await expect(page.getByText(/National Health Insurance/i).first()).toBeVisible({ timeout: 15000 });
      }
      if (route.path === '/users') {
        // Users page should render successfully without isBackendReachable ReferenceError
        await expect(page.getByText(/Users/i).first()).toBeVisible({ timeout: 10000 });
        await expect(page.getByPlaceholder(/Search by name/i)).toBeVisible({ timeout: 10000 });
      }

      // Ensure no blank screen
      const bodyText = await page.innerText('body');
      expect(bodyText.length).toBeGreaterThan(50);
    }

    console.log('[E2E-AUDIT] All routes verified cleanly in both ONLINE and OFFLINE modes!');
  });
});
