import { test, expect } from '@playwright/test';

test('test baseURL', async ({ page }) => {
  await page.goto('/pos');
  await expect(page.locator('body')).toBeVisible();
});