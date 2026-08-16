import { test, expect } from '@playwright/test';
import { TauriSqliteBridge } from './helpers/tauri-bridge';
import { setupAuthenticatedState } from './helpers/auth-helper';
import { BackendDatabase } from './helpers/backend-db';

test.describe('Navigation Lifecycle & Global Reload Verification', () => {
  let bridge: TauriSqliteBridge;
  let backendDb: BackendDatabase;

  test.beforeEach(async ({ page }) => {
    test.setTimeout(60000);
    backendDb = new BackendDatabase();
    bridge = new TauriSqliteBridge();
    await bridge.attachToPage(page);
    const auth = await setupAuthenticatedState(page, bridge);
    await backendDb.seedDefaultPriceContract(auth.orgId);
  });

  test.afterEach(async () => {
    await backendDb.close();
  });

  test('Seamless client-side navigation without app reload, sync flash, or loading flicker', async ({ page }) => {
    const consoleLogs: string[] = [];
    const localReadLogs: { searchDrugs: number; getBranchInventory: number } = {
      searchDrugs: 0,
      getBranchInventory: 0,
    };

    page.on('console', (msg) => {
      const text = msg.text();
      consoleLogs.push(text);
      if (text.includes('[LocalRead] searchDrugs: search=')) {
        localReadLogs.searchDrugs++;
      }
      if (text.includes('[LocalRead] getBranchInventory: branch=')) {
        localReadLogs.getBranchInventory++;
      }
    });

    // 1. Initial Load to /pos
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');

    // Wait until AppShell and POS page are mounted
    await expect(page.locator('aside')).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Point of Sale').first()).toBeVisible();

    // Set a window canary to strictly verify that no full page document reload occurs
    const canaryToken = `canary-${Date.now()}-${Math.random()}`;
    await page.evaluate((token) => {
      (window as any).__navigation_canary = token;
    }, canaryToken);

    // Trigger initial sync to reach stable idle state
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });
    await expect(page.getByText(/Just now|\d+m ago|0 pending/i).first()).toBeVisible({ timeout: 20000 });

    // Active Branch must be visible and stable
    const branchPill = page.locator('aside').getByText('Downtown Main Branch');
    await expect(branchPill).toBeVisible();
    await expect(page.locator('aside').getByText('Active Branch')).toBeVisible();

    // 2. Navigate to Audit Logs
    const auditLogsLink = page.locator('aside nav a[href="/audit-logs"]');
    await auditLogsLink.click();

    // Verify Audit Logs header appears
    await expect(page.locator('h1').getByText('Audit Logs')).toBeVisible({ timeout: 10000 });

    // Canary must survive (no document reload)
    const canaryAfterAudit = await page.evaluate(() => (window as any).__navigation_canary);
    expect(canaryAfterAudit).toBe(canaryToken);

    // Active branch must remain unchanged and never show "Loading..."
    await expect(branchPill).toBeVisible();
    expect(await page.locator('aside').getByText('Loading…').count()).toBe(0);

    // Global SyncGate modal must NEVER appear during navigation
    expect(await page.getByText('Syncing your data…').count()).toBe(0);
    expect(await page.getByText('Fetching latest drugs, inventory and contracts').count()).toBe(0);

    // Never synced warning must NOT appear
    expect(await page.getByText('Never synced').count()).toBe(0);
    expect(await page.getByText(/No data has synced to this device yet/).count()).toBe(0);

    // 3. Navigate to Users
    const usersLink = page.locator('aside nav a[href="/users"]');
    await usersLink.click();
    await expect(page.locator('h1').getByText('Users')).toBeVisible({ timeout: 10000 });

    // Canary check
    const canaryAfterUsers = await page.evaluate(() => (window as any).__navigation_canary);
    expect(canaryAfterUsers).toBe(canaryToken);
    await expect(branchPill).toBeVisible();
    expect(await page.getByText('Syncing your data…').count()).toBe(0);
    expect(await page.getByText('Never synced').count()).toBe(0);

    // 4. Navigate to Settings
    const settingsLink = page.locator('aside nav a[href="/settings"]');
    await settingsLink.click();
    await expect(page.locator('h1').getByText('Settings')).toBeVisible({ timeout: 10000 });

    // Canary check
    const canaryAfterSettings = await page.evaluate(() => (window as any).__navigation_canary);
    expect(canaryAfterSettings).toBe(canaryToken);
    await expect(branchPill).toBeVisible();
    expect(await page.getByText('Syncing your data…').count()).toBe(0);
    expect(await page.getByText('Never synced').count()).toBe(0);

    // 5. Navigate back to Audit Logs
    await auditLogsLink.click();
    await expect(page.locator('h1').getByText('Audit Logs')).toBeVisible({ timeout: 10000 });

    // Canary check
    const canaryAfterAudit2 = await page.evaluate(() => (window as any).__navigation_canary);
    expect(canaryAfterAudit2).toBe(canaryToken);
    await expect(branchPill).toBeVisible();
    expect(await page.getByText('Syncing your data…').count()).toBe(0);
    expect(await page.getByText('Never synced').count()).toBe(0);
  });
});
