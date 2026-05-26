import { beforeEach, describe, expect, it, vi } from 'vitest';
import { organizationApi } from '../organization';
import { get, patch } from '../client';

vi.mock('../client', () => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
}));

describe('organizationApi settings endpoints', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('fetches organization stats through the canonical organization API', async () => {
    vi.mocked(get).mockResolvedValueOnce({ organization_id: 'org-1', total_branches: 2 });

    const result = await organizationApi.getStats('org-1');

    expect(get).toHaveBeenCalledWith('/organizations/org-1/stats', { signal: undefined });
    expect(result).toEqual({ organization_id: 'org-1', total_branches: 2 });
  });

  it('updates operational settings on the canonical settings endpoint', async () => {
    vi.mocked(patch).mockResolvedValueOnce({ id: 'org-1', settings: { currency: 'GHS' } });

    const result = await organizationApi.updateSettings('org-1', {
      currency: 'GHS',
      low_stock_threshold: 12,
    });

    expect(patch).toHaveBeenCalledWith('/organizations/org-1/settings', {
      currency: 'GHS',
      low_stock_threshold: 12,
    });
    expect(result).toEqual({ id: 'org-1', settings: { currency: 'GHS' } });
  });
});
