import { describe, expect, it } from 'vitest';
import {
  getHomePathForRole,
  parseAdminTab,
  parseSettingsTab,
} from '@/lib/routes';

describe('route helpers', () => {
  it('sends management roles to admin and operational roles to POS', () => {
    expect(getHomePathForRole('admin')).toBe('/admin');
    expect(getHomePathForRole('super_admin')).toBe('/admin');
    expect(getHomePathForRole('manager')).toBe('/admin');
    expect(getHomePathForRole('cashier')).toBe('/pos');
    expect(getHomePathForRole(undefined)).toBe('/pos');
  });

  it('normalizes admin tabs', () => {
    expect(parseAdminTab('inventory')).toBe('inventory');
    expect(parseAdminTab('contracts')).toBe('contracts');
    expect(parseAdminTab('old-drug-management')).toBe('drugs');
    expect(parseAdminTab()).toBe('drugs');
  });

  it('normalizes settings tabs', () => {
    expect(parseSettingsTab('branches')).toBe('branches');
    expect(parseSettingsTab('organization')).toBe('organization');
    expect(parseSettingsTab('organization-stats')).toBe('organization');
    expect(parseSettingsTab()).toBe('organization');
  });
});
