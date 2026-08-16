import { Page, expect } from '@playwright/test';
import { TauriSqliteBridge } from './tauri-bridge';

export async function loginViaUI(
  page: Page,
  username = 'admin',
  password = 'Password123!'
) {
  await page.goto('/login');
  
  // Wait for login form
  const usernameInput = page.locator('input[name="username"], input[type="text"]').first();
  const passwordInput = page.locator('input[name="password"], input[type="password"]').first();
  
  await usernameInput.fill(username);
  await passwordInput.fill(password);
  
  const submitButton = page.locator('button[type="submit"]').first();
  await submitButton.click();
  
  // Wait for redirect to POS or home
  await page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 10000 });
}

export async function setupAuthenticatedState(
  page: Page,
  bridge: TauriSqliteBridge,
  username = 'admin',
  password = 'Password123!'
) {
  // Call backend login directly to get valid JWT and user payload
  const res = await page.request.post('http://127.0.0.1:8000/api/v1/auth/login', {
    data: {
      username,
      password,
    },
  });
  
  if (!res.ok()) {
    throw new Error(`Auth login failed: ${res.status()} ${await res.text()}`);
  }
  
  const data = await res.json();
  const token = data.access_token;
  const user = data.user;
  const branchId = user.branch_id || (user.assigned_branches && user.assigned_branches[0]) || '22222222-2222-2222-2222-222222222222';
  const orgId = user.organization_id || '11111111-1111-1111-1111-111111111111';

  bridge.secureStore.set('auth.access_token', token);
  bridge.secureStore.set('auth.user', user);
  bridge.secureStore.set('session.branch_id', branchId);
  bridge.secureStore.set('session.organization_id', orgId);
  bridge.secureStore.set('cache.organization', { id: orgId, name: 'Demo Pharmacy Org' });
  bridge.secureStore.set('cache.branches', [{ id: branchId, name: 'Downtown Main Branch', code: 'DT01' }]);
  if (data.refresh_token) {
    bridge.secureStore.set('auth.refresh_token', data.refresh_token);
  }

  // Pre-seed localStorage/sessionStorage
  await page.addInitScript(({ token, user, branchId, orgId }) => {
    sessionStorage.setItem('auth.access_token', JSON.stringify(token));
    localStorage.setItem('auth.user', JSON.stringify(user));
    localStorage.setItem('session.branch_id', JSON.stringify(branchId));
    localStorage.setItem('session.organization_id', JSON.stringify(orgId));
    localStorage.setItem('auth.active_branch_id', JSON.stringify(branchId));
    localStorage.setItem('auth.active_organization_id', JSON.stringify(orgId));
  }, { token, user, branchId, orgId });

  return { token, user, branchId, orgId };
}
