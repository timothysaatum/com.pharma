/**
 * Reports API Client
 * Frontend HTTP client for reports endpoints
 */

import { get } from './client';
import type { PaginatedResponse } from '@/types';

export interface DailySalesFilter {
  startDate: string;
  endDate: string;
  branchId?: string;
  contractId?: string;
  cashierId?: string;
  page?: number;
  page_size?: number;
}

export interface ContractPerformanceFilter {
  startDate: string;
  endDate: string;
  contractId?: string;
}

export interface InventoryAlertsFilter {
  branchId?: string;
  alertTypes?: string;
}

export interface ContractPerformanceRow {
  contract_id: string;
  contract_code: string;
  contract_name: string;
  contract_type: string;
  sales_count: number;
  revenue: number;
  discount_given: number;
  avg_discount: number;
  customer_count: number;
}

export interface TopCustomersFilter {
  startDate: string;
  endDate: string;
  limit?: number;
}

export interface DrugTurnoverFilter {
  startDate: string;
  endDate: string;
  branchId?: string;
  page?: number;
  page_size?: number;
}

class ReportsAPI {
  async getDailySalesSummary(filters: DailySalesFilter) {
    const params = new URLSearchParams({
      start_date: filters.startDate,
      end_date: filters.endDate,
      page: (filters.page || 1).toString(),
      page_size: (filters.page_size || 50).toString(),
    });

    if (filters.branchId) params.append('branch_id', filters.branchId);
    if (filters.contractId) params.append('contract_id', filters.contractId);
    if (filters.cashierId) params.append('cashier_id', filters.cashierId);

    const response = await get<PaginatedResponse<any>>(`/reports/daily-sales-summary?${params}`);
    return response;
  }

  async getContractPerformance(filters: ContractPerformanceFilter) {
    const params = new URLSearchParams({
      start_date: filters.startDate,
      end_date: filters.endDate,
    });

    if (filters.contractId) params.append('contract_id', filters.contractId);

    const response = await get<ContractPerformanceRow[]>(`/reports/contract-performance?${params}`);
    return response;
  }

  async getInventoryAlerts(filters: InventoryAlertsFilter) {
    const params = new URLSearchParams();

    if (filters.branchId) params.append('branch_id', filters.branchId);
    if (filters.alertTypes) params.append('alert_types', filters.alertTypes);

    const response = await get<any[]>(`/reports/inventory-alerts?${params}`);
    return response;
  }

  async getTopCustomers(filters: TopCustomersFilter) {
    const params = new URLSearchParams({
      start_date: filters.startDate,
      end_date: filters.endDate,
      limit: (filters.limit || 10).toString(),
    });

    const response = await get<any[]>(`/reports/top-customers?${params}`);
    return response;
  }

  async getDrugTurnover(filters: DrugTurnoverFilter) {
    const params = new URLSearchParams({
      start_date: filters.startDate,
      end_date: filters.endDate,
      page: (filters.page || 1).toString(),
      page_size: (filters.page_size || 20).toString(),
    });

    if (filters.branchId) params.append('branch_id', filters.branchId);

    const response = await get<PaginatedResponse<any>>(`/reports/drug-turnover?${params}`);
    return response;
  }
}

export const reportsApi = new ReportsAPI();
