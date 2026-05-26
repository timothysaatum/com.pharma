/** @vitest-environment jsdom */
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { vi, describe, it, expect } from 'vitest';

// Mock reportsApi
vi.mock('@/api/reports', () => ({
  reportsApi: {
    getDailySalesSummary: () => Promise.resolve([
      {
        sale_date: '2026-05-24',
        branch_name: 'Main Branch',
        net_revenue: 171.0,
        total_items: 6,
        transaction_count: 4
      }
    ]),
    getContractPerformance: () => Promise.resolve([
      {
        contract_id: '1',
        contract_name: 'Standard',
        revenue: 100.0,
        discount_given: 5.0,
        customer_count: 10,
      }
    ])
  }
}));

import ReportsPage from '../ReportsPage';

const queryClient = new QueryClient();

describe('ReportsPage', () => {
  it('shows daily sales and uses total_items', async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ReportsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => expect(screen.getByText('Daily Sales')).toBeTruthy());

    // Expect the sample row values to appear
    expect(await screen.findByText('2026-05-24')).toBeTruthy();
    expect(await screen.findByText('6')).toBeTruthy(); // items
    expect(await screen.findByText('4')).toBeTruthy(); // transactions
  });
});
