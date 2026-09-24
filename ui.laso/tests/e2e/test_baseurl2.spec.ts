import { test, expect } from '@playwright/test';

test.use({ baseURL: 'http://127.0.0.1:1420' });

test('test baseURL explicit', async ({ page }) => {
  await page.goto('/pos');
  await expect(page.locator('body')).toBeVisible();
});
