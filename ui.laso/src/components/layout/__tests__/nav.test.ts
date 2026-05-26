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
  });
});
