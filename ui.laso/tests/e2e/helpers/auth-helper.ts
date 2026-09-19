import { Page, expect } from '@playwright/test';
import { TauriSqliteBridge } from './tauri-bridge';

// Module-level cache to avoid hitting the auth rate limit (5 req/60s).
// Token is cached for 2 minutes — well within JWT lifetime but safely under
// the burst budget across sequential test runs.
let _authCache: {
  token: string;
  user: any;
  branchId: string;
  orgId: string;
  refreshToken?: string;
  fetchedAt: number;
} | null = null;
const AUTH_CACHE_TTL_MS = 120_000;

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
  let token: string;
  let user: any;
  let branchId: string;
  let orgId: string;
  let refreshToken: string | undefined;

  const now = Date.now();
  if (_authCache && now - _authCache.fetchedAt < AUTH_CACHE_TTL_MS) {
    ({ token, user, branchId, orgId, refreshToken } = _authCache);
  } else {
    // Call backend login directly to get valid JWT and user payload (with retry for transient ECONNRESET)
    let res;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        res = await page.request.post('http://127.0.0.1:8000/api/v1/auth/login', {
          data: { username, password },
        });
        if (res.ok()) break;
        // On 429, wait longer before retrying so we don't compound rate-limit pressure
        if (res.status() === 429 && attempt < 2) {
          await new Promise(r => setTimeout(r, 65_000));
        }
      } catch (err) {
        if (attempt === 2) throw err;
        await new Promise(r => setTimeout(r, 200 * (attempt + 1)));
      }
    }

    if (!res || !res.ok()) {
      throw new Error(`Auth login failed: ${res ? res.status() : 'no response'}`);
    }

    const data = await res.json();
    token = data.access_token;
    user = data.user;
    branchId = user.branch_id || (user.assigned_branches && user.assigned_branches[0]) || '22222222-2222-2222-2222-222222222222';
    orgId = user.organization_id || '11111111-1111-1111-1111-111111111111';
    refreshToken = data.refresh_token;
    _authCache = { token, user, branchId, orgId, refreshToken, fetchedAt: now };
  }

  bridge.secureStore.set('auth.access_token', token);
  bridge.secureStore.set('auth.user', user);
  bridge.secureStore.set('session.branch_id', branchId);
  bridge.secureStore.set('session.organization_id', orgId);
  bridge.secureStore.set('cache.organization', { id: orgId, name: 'Demo Pharmacy Org' });
  bridge.secureStore.set('cache.branches', [{ id: branchId, name: 'Downtown Main Branch', code: 'DT01' }]);
  if (refreshToken) {
    bridge.secureStore.set('auth.refresh_token', refreshToken);
  }

  // Pre-seed localStorage/sessionStorage
  await page.addInitScript(({ token, user, branchId, orgId }) => {
    sessionStorage.setItem('auth.access_token', JSON.stringify(token));
    localStorage.setItem('auth.user', JSON.stringify(user));
    localStorage.setItem('session.branch_id', JSON.stringify(branchId));
    localStorage.setItem('session.organization_id', JSON.stringify(orgId));
    localStorage.setItem('auth.active_branch_id', JSON.stringify(branchId));
    localStorage.setItem('auth.active_organization_id', JSON.stringify(orgId));
    localStorage.setItem(`cache.branch_name.${branchId}`, 'Downtown Main Branch');
    localStorage.setItem(`cache.org_name.${orgId}`, 'Demo Pharmacy Org');
  }, { token, user, branchId, orgId });

  return { token, user, branchId, orgId };
}
