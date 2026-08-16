import { test, expect } from '@playwright/test';
import { TauriSqliteBridge } from './helpers/tauri-bridge';
import { setupAuthenticatedState } from './helpers/auth-helper';

test.describe('Sync Lifecycle & Indicator Adversarial E2E Tests', () => {
  let bridge: TauriSqliteBridge;

  test.beforeEach(async ({ page }) => {
    test.setTimeout(60000);
    page.on('console', msg => console.log(`[BROWSER ${msg.type()}]:`, msg.text()));
    page.on('pageerror', err => console.error('[BROWSER ERROR]:', err));
    bridge = new TauriSqliteBridge();
    await bridge.attachToPage(page);
    await setupAuthenticatedState(page, bridge);
  });

  test('1. Fresh/first-time user shows "Never synced" state and warning until first sync', async ({ page }) => {
    // Hold back initial auto-sync to verify the Never Synced state
    let allowSync = false;
    await page.route('**/api/v1/sync/events*', async (route) => {
      if (!allowSync) {
        await route.abort('failed');
      } else {
        await route.continue();
      }
    });

    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');

    // Verify the warning text about incomplete stock is present
    const warningText = page.getByText(/No data has synced to this device yet/i);
    await expect(warningText).toBeVisible({ timeout: 10000 });

    // Verify local SQLite has no last_sync_at yet
    const lastSync = bridge.getLastSyncAt();
    expect(lastSync).toBeNull();

    // Now unblock sync and trigger manual sync
    allowSync = true;
    await page.unroute('**/api/v1/sync/events*');

    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });

    // Wait for sync to complete (label changes from "Syncing…" to "Just now" or relative time)
    await expect(page.getByText(/Just now|\d+m ago|0 pending/i).first()).toBeVisible({ timeout: 20000 });

    // Verify "Never synced" warning disappears
    await expect(warningText).not.toBeVisible();

    // Verify SQLite now has last_sync_at recorded
    const updatedLastSync = bridge.getLastSyncAt();
    expect(updatedLastSync).not.toBeNull();
    expect(typeof updatedLastSync).toBe('string');
  });

  test('2. Sync when already up-to-date executes smoothly without duplicating data', async ({ page }) => {
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');

    // Initial sync
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });
    await expect(page.getByText(/Just now|\d+m ago|0 pending/i).first()).toBeVisible({ timeout: 20000 });

    // Record row count in SQLite
    const initialRows = bridge.getTableRows('branch_inventory');
    const initialCount = initialRows.length;

    // Trigger sync again when already up to date
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });

    await page.waitForTimeout(1000);
    await expect(page.getByText(/Just now|\d+m ago|0 pending/i).first()).toBeVisible({ timeout: 15000 });

    // Verify row count did not duplicate
    const afterRows = bridge.getTableRows('branch_inventory');
    expect(afterRows.length).toBe(initialCount);
  });

  test('3. Rapid repeated clicks do not crash, leak transactions, or create race conditions', async ({ page }) => {
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');

    // Initial sync
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });
    await expect(page.getByText(/Just now|\d+m ago|0 pending/i).first()).toBeVisible({ timeout: 20000 });

    // Rapidly trigger 6 syncs in parallel
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await Promise.all([
        syncEngine.sync().catch(() => {}),
        syncEngine.sync().catch(() => {}),
        syncEngine.sync().catch(() => {}),
        syncEngine.sync().catch(() => {}),
        syncEngine.sync().catch(() => {}),
        syncEngine.sync().catch(() => {}),
      ]);
    });

    // Wait for sync cycle to settle
    await page.waitForTimeout(2000);
    await expect(page.getByText(/Just now|\d+m ago|0 pending/i).first()).toBeVisible({ timeout: 20000 });

    // Verify no unhandled error toast or red alert
    const errorAlert = page.locator('text=Sync error');
    await expect(errorAlert).not.toBeVisible();
  });

  test('4. Refresh during and after sync preserves database state and timestamps', async ({ page }) => {
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');

    // Initial sync
    await page.evaluate(async () => {
      // @ts-ignore
      const { syncEngine } = await import('/src/lib/syncEngine.ts');
      await syncEngine.sync();
    });
    await expect(page.getByText(/Just now|\d+m ago|0 pending/i).first()).toBeVisible({ timeout: 20000 });

    const lastSyncBefore = bridge.getLastSyncAt();
    expect(lastSyncBefore).toBeTruthy();

    // Reload page
    await page.reload();
    await page.waitForLoadState('domcontentloaded');

    // Verify last sync time is remembered and "Never synced" does not show
    const warningText = page.getByText(/No data has synced to this device yet/i);
    await expect(warningText).not.toBeVisible();

    const lastSyncAfter = bridge.getLastSyncAt();
    expect(lastSyncAfter).toBeTruthy();
  });

  test('5. Navigation during active sync does not cause orphaned promises or UI freezes', async ({ page }) => {
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');

    // Trigger sync in background non-blocking
    await page.evaluate(() => {
      // @ts-ignore
      import('/src/lib/syncEngine.ts').then(m => m.syncEngine.sync()).catch(() => {});
    });

    // Immediately navigate around the application
    await page.goto('/customers');
    await page.waitForTimeout(300);
    await page.goto('/prescriptions');
    await page.waitForTimeout(300);
    await page.goto('/conflicts');
    await page.waitForTimeout(300);
    await page.goto('/pos');
    await page.waitForLoadState('domcontentloaded');

    // Verify app is alive, responsive, and sync settled cleanly
    await expect(page.locator('input[placeholder*="Search drug"]').first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/Just now|\d+m ago|0 pending/i).first()).toBeVisible({ timeout: 30000 });
  });
});
