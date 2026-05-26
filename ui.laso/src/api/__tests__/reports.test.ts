import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

vi.mock('../client', () => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  del: vi.fn(),
}));

import { reportsApi, type ContractPerformanceRow } from '../reports';
import { get as mockedGet } from '../client';

const typedGet = mockedGet as unknown as Mock;

describe('reportsApi', () => {
  beforeEach(() => {
    typedGet.mockReset();
  });

  it('calls contract performance endpoint with contractId when provided', async () => {
    const payload: ContractPerformanceRow[] = [
      {
        contract_id: 'contract-1',
        contract_code: 'C-001',
        contract_name: 'Corporate',
        contract_type: 'corporate',
        sales_count: 5,
        revenue: 500.0,
        discount_given: 25.0,
        avg_discount: 5.0,
        customer_count: 4,
      },
    ];

    typedGet.mockResolvedValueOnce(payload);

    const result = await reportsApi.getContractPerformance({
      startDate: '2025-01-01',
      endDate: '2025-01-31',
      contractId: 'contract-1',
    });

    expect(typedGet).toHaveBeenCalledTimes(1);
    expect(typedGet).toHaveBeenCalledWith('/reports/contract-performance?start_date=2025-01-01&end_date=2025-01-31&contract_id=contract-1');
    expect(result).toBe(payload);
  });

  it('calls daily sales endpoint with optional branch and cashier params', async () => {
    const payload = [
      {
        sale_date: '2025-02-10',
        branch_id: 'branch-1',
        branch_name: 'Main Branch',
        price_contract_id: null,
        contract_name: null,
        cashier_id: 'cashier-1',
        cashier_name: 'Jane Doe',
        transaction_count: 7,
        gross_revenue: 420,
        total_discount: 20,
        total_tax: 30,
        net_revenue: 430,
        total_items: 42,
        refund_count: 0,
      },
    ];

    typedGet.mockResolvedValueOnce(payload);

    const result = await reportsApi.getDailySalesSummary({
      startDate: '2025-02-01',
      endDate: '2025-02-28',
      branchId: 'branch-1',
      contractId: 'contract-1',
      cashierId: 'cashier-1',
    });

    expect(typedGet).toHaveBeenCalledTimes(1);
    expect(typedGet).toHaveBeenCalledWith('/reports/daily-sales-summary?start_date=2025-02-01&end_date=2025-02-28&branch_id=branch-1&contract_id=contract-1&cashier_id=cashier-1');
    expect(result).toBe(payload);
  });
});
