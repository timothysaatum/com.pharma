import { describe, it, expect } from 'vitest';
import { NAV_ITEMS } from '@/components/layout/AppShell';

describe('AppShell navigation', () => {
  it('includes admin and excludes consolidated routes', () => {
    const paths = NAV_ITEMS.map((i) => i.to);
    expect(paths).toContain('/admin');
    expect(paths).not.toContain('/drugs');
    expect(paths).not.toContain('/inventory');
    expect(paths).not.toContain('/purchases');
    expect(paths).not.toContain('/contracts');
    expect(paths).not.toContain('/organization-stats');
    expect(paths).not.toContain('/branches');
    expect(paths).not.toContain('/drug-management');
  });

  it('keeps organization and branch management under settings', () => {
    const settings = NAV_ITEMS.find((i) => i.to === '/settings');
    expect(settings?.roles).toEqual(['admin', 'super_admin']);
  });
});
