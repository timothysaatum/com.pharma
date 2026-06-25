/**
 * Reports Page Component
 * Comprehensive analytics and reporting interface
 * Supports: Daily Sales, Contracts, Inventory Alerts, Customers, Drug Turnover
 */

import { useState, useCallback, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { BarChart2, Download, RefreshCw, AlertCircle, ChevronLeft, ChevronRight, Building2, FileText } from 'lucide-react';
import { format, subDays } from 'date-fns';
import { reportsApi } from '../api/reports';
import { branchApi } from '@/api/branches';
import { contractsApi } from '@/api/contracts';
import { isOfflineError } from '@/api/client';
import { localRead } from '@/lib/localRead';
import { offlineCache } from '@/lib/storage';
import { DataFreshnessIndicator } from '@/components/DataFreshnessIndicator';
import type { PaginatedResponse } from '@/types';

interface FilterState {
  startDate: string;
  endDate: string;
  branchId?: string;
  contractId?: string;
  cashierId?: string;
  page?: number;
  page_size?: number;
}

interface DailySalesRow {
  sale_date: string;
  branch_id: string | null;
  branch_name: string | null;
  price_contract_id: string | null;
  contract_name: string | null;
  cashier_id: string | null;
  cashier_name: string | null;
  transaction_count: number;
  gross_revenue: number;
  total_discount: number;
  total_tax: number;
  net_revenue: number;
  total_items: number;
  refund_count: number;
}

interface ContractPerformanceRow {
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

interface InventoryAlertRow {
  drug_name: string;
  alert_type: string;
  message: string;
}

export default function ReportsPage() {
  const [activeTab, setActiveTab] = useState<'daily-sales' | 'contracts' | 'inventory' | 'drugs'>('daily-sales');
  const [filters, setFilters] = useState<FilterState>({
    startDate: format(subDays(new Date(), 30), 'yyyy-MM-dd'),
    endDate: format(new Date(), 'yyyy-MM-dd'),
    page: 1,
    page_size: 50,
  });

  const [showFilters] = useState(true);
  const [dailySalesFromCache, setDailySalesFromCache] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [branches, setBranches] = useState<{ id: string; name: string }[]>([]);
  const [contracts, setContracts] = useState<{ id: string; name: string; code: string }[]>([]);
  const [filterLoading, setFilterLoading] = useState(true);

  // Daily Sales Query
  const { data: dailySalesPaginated, isFetching: dailySalesLoading, refetch: refetchDailySales } = useQuery<PaginatedResponse<DailySalesRow>>({
    queryKey: ['reports', 'daily-sales', filters],
    queryFn: async () => {
      setDailySalesFromCache(false);
      try {
        return await reportsApi.getDailySalesSummary({
          startDate: filters.startDate,
          endDate: filters.endDate,
          branchId: filters.branchId,
          contractId: filters.contractId,
          cashierId: filters.cashierId,
          page: filters.page,
          page_size: filters.page_size,
        });
      } catch (err) {
        // If offline, fall back to local DB aggregation
        if (isOfflineError(err)) {
          setDailySalesFromCache(true);
          // fetch all sales from local DB within date range
          const localPageSize = 1000;
          const local = await localRead.searchSales({
            start_date: filters.startDate,
            end_date: filters.endDate,
            branch_id: filters.branchId,
          }, 1, localPageSize);

          // Enrich local sales with items count and branch name
          const rows: DailySalesRow[] = [];
          for (const s of local.items) {
            try {
              const details = await localRead.getSaleById(s.id);
              const itemsCount =
                details?.items_count ??
                details?.items?.reduce((sum, item) => sum + (item?.quantity ?? 0), 0) ??
                0;
              let branchName: string | null = null;
              if (s.branch_id) {
                branchName = await offlineCache.getBranchName(s.branch_id);
              }

              rows.push({
                sale_date: (s.created_at || '').slice(0, 10),
                branch_id: s.branch_id ?? null,
                branch_name: branchName,
                price_contract_id: s.price_contract_id ?? null,
                contract_name: s.contract_name ?? null,
                cashier_id: s.cashier_id ?? null,
                cashier_name: null,
                transaction_count: 1,
                gross_revenue: s.total_amount ?? 0,
                total_discount: s.discount_amount ?? 0,
                total_tax: s.tax_amount ?? 0,
                net_revenue: s.total_amount ?? 0,
                total_items: itemsCount,
                refund_count: 0,
              });
            } catch (e) {
              // skip problematic rows
            }
          }

          // aggregate by date
          const grouped: Record<string, DailySalesRow> = {};
          for (const r of rows) {
            const key = r.sale_date;
            if (!grouped[key]) {
              grouped[key] = { ...r };
            } else {
              grouped[key].transaction_count += r.transaction_count;
              grouped[key].gross_revenue += r.gross_revenue;
              grouped[key].total_discount += r.total_discount;
              grouped[key].total_tax += r.total_tax;
              grouped[key].net_revenue += r.net_revenue;
              grouped[key].total_items += r.total_items;
              grouped[key].refund_count += r.refund_count;
            }
          }

          const allRows = Object.keys(grouped).sort().reverse().map((k) => grouped[k]);
          const page = filters.page || 1;
          const pageSize = filters.page_size || 50;
          const start = (page - 1) * pageSize;
          const paginatedRows = allRows.slice(start, start + pageSize);

          return {
            items: paginatedRows,
            total: allRows.length,
            page,
            page_size: pageSize,
            total_pages: Math.ceil(allRows.length / pageSize),
            has_next: start + pageSize < allRows.length,
            has_prev: page > 1,
          };
        }

        // rethrow other errors
        throw err;
      }
    },
    enabled: activeTab === 'daily-sales',
  });
  const dailySalesData = dailySalesPaginated?.items || [];

  // Contract Performance Query
  const { data: contractData, isLoading: contractLoading } = useQuery<ContractPerformanceRow[]>({
    queryKey: ['reports', 'contracts', filters],
    queryFn: () => reportsApi.getContractPerformance({
      startDate: filters.startDate,
      endDate: filters.endDate,
      contractId: filters.contractId,
    }),
    enabled: activeTab === 'contracts',
  });

  // Inventory Alerts Query
  const { data: inventoryData, isLoading: inventoryLoading } = useQuery<InventoryAlertRow[]>({
    queryKey: ['reports', 'inventory', filters],
    queryFn: () => reportsApi.getInventoryAlerts({
      branchId: filters.branchId,
      alertTypes: filters.branchId ? 'low_stock,expiring,expired' : undefined,
    }),
    enabled: activeTab === 'inventory',
  });

  // Drug Turnover Query
  const { data: drugTurnoverPaginated, isLoading: drugTurnoverLoading } = useQuery<PaginatedResponse<any>>({
    queryKey: ['reports', 'drug-turnover', filters],
    queryFn: () => reportsApi.getDrugTurnover({
      startDate: filters.startDate,
      endDate: filters.endDate,
      branchId: filters.branchId,
      page: filters.page,
      page_size: filters.page_size || 20,
    }),
    enabled: activeTab === 'drugs',
  });
  const drugTurnoverData = drugTurnoverPaginated?.items || [];

  const handleFilterChange = useCallback((key: keyof FilterState, value: any) => {
    setFilters(prev => ({
      ...prev,
      [key]: value,
      page: (key === 'page' || key === 'page_size') ? value : 1, // Reset to page 1 on filter changes except page itself
    }));
  }, []);

  useEffect(() => {
    setFilters(prev => ({ ...prev, page: 1 }));
  }, [activeTab]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setFilterLoading(true);
      try {
        const [branchRes, contractRes] = await Promise.all([
          branchApi.list({ page: 1, page_size: 100 }),
          contractsApi.list({ page: 1, page_size: 100 }),
        ]);
        if (!cancelled) {
          setBranches((branchRes as any).items?.map((b: any) => ({ id: b.id, name: b.name })) ?? []);
          setContracts((contractRes as any).contracts?.map((c: any) => ({ id: c.id, name: c.contract_name, code: c.contract_code })) ?? []);
        }
      } catch {
        // non-critical — filters will just show "All"
      } finally {
        if (!cancelled) setFilterLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, []);

  const exportToCSV = (data: any[], filename: string) => {
    if (!data || data.length === 0) return;

    const headers = Object.keys(data[0]);
    const csv = [
      headers.join(','),
      ...data.map(row =>
        headers.map(header => {
          const value = row[header];
          return typeof value === 'string' && value.includes(',') ? `"${value}"` : value;
        }).join(',')
      ),
    ].join('\n');

    const blob = new Blob([csv], { type: 'text/csv' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${filename}-${format(new Date(), 'yyyy-MM-dd')}.csv`;
    a.click();
    window.URL.revokeObjectURL(url);
  };

  const renderDailySalesTab = () => (
    <div className="space-y-6">
      {/* Filters */}
      {showFilters && (
        <div className="bg-white p-4 rounded-lg border border-gray-200 space-y-4">
          <h3 className="font-semibold text-gray-900">Filters</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Start Date</label>
              <input
                type="date"
                value={filters.startDate}
                onChange={(e) => handleFilterChange('startDate', e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">End Date</label>
              <input
                type="date"
                value={filters.endDate}
                onChange={(e) => handleFilterChange('endDate', e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Branch</label>
              <select
                value={filters.branchId || ''}
                onChange={(e) => handleFilterChange('branchId', e.target.value || undefined)}
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm"
              >
                <option value="">All Branches</option>
                {branches.map(b => (
                  <option key={b.id} value={b.id}>{b.name}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Contract</label>
              <select
                value={filters.contractId || ''}
                onChange={(e) => handleFilterChange('contractId', e.target.value || undefined)}
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm"
              >
                <option value="">All Contracts</option>
                {contracts.map(c => (
                  <option key={c.id} value={c.id}>{c.code} — {c.name}</option>
                ))}
              </select>
            </div>
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={dailySalesLoading}
              onClick={() => {
                refetchDailySales();
              }}
              className="px-3 py-1.5 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1.5 text-sm transition-colors"
              title="Refresh data"
            >
              <RefreshCw size={14} className={dailySalesLoading ? "animate-spin" : ""} />
              {dailySalesLoading ? "Refreshing..." : "Refresh"}
            </button>
            <button
              type="button"
              onClick={() => {
                if (dailySalesData && dailySalesData.length > 0) {
                  setIsExporting(true);
                  setTimeout(() => {
                    exportToCSV(dailySalesData, 'daily-sales');
                    setIsExporting(false);
                  }, 500);
                }
              }}
              disabled={!dailySalesData || dailySalesData.length === 0 || isExporting}
              className="px-3 py-1.5 bg-green-600 text-white rounded-md hover:bg-green-700 disabled:bg-gray-400 disabled:cursor-not-allowed flex items-center gap-1.5 text-sm transition-colors"
              title="Export data to CSV"
            >
              {isExporting ? <RefreshCw size={14} className="animate-spin" /> : <Download size={14} />}
              {isExporting ? "Exporting..." : "Export CSV"}
            </button>
          </div>
        </div>
      )}

      {/* Data Table */}
      {dailySalesFromCache && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-4 text-amber-900">
          <DataFreshnessIndicator isFromCache cached_at={new Date().toISOString()} compact />
        </div>
      )}
      {dailySalesLoading ? (
        <div className="bg-white p-8 rounded-lg text-center text-gray-500">Loading sales data...</div>
      ) : dailySalesData && dailySalesData.length > 0 ? (
        <div className="space-y-4">
          <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
            <table className="w-full">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-700 uppercase">Date</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-700 uppercase">Branch</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-700 uppercase">Revenue</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-700 uppercase">Items</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-700 uppercase">Transactions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200">
                {dailySalesData.map((row: any, idx: number) => (
                  <tr key={idx} className="hover:bg-gray-50">
                    <td className="px-6 py-3 text-sm text-gray-900">{row.sale_date}</td>
                    <td className="px-6 py-3 text-sm text-gray-900">{row.branch_name || '-'}</td>
                    <td className="px-6 py-3 text-sm font-semibold text-gray-900">₵{parseFloat((row.net_revenue ?? row.gross_revenue) || 0).toFixed(2)}</td>
                    <td className="px-6 py-3 text-sm text-gray-900">{row.total_items || 0}</td>
                    <td className="px-6 py-3 text-sm text-gray-900">{row.transaction_count || 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* Pagination Controls */}
          {dailySalesPaginated && dailySalesPaginated.total_pages > 1 && (
            <div className="flex items-center justify-between bg-white px-4 py-3 border border-gray-200 rounded-lg">
              <div className="text-sm text-gray-700">
                Showing page <span className="font-medium">{dailySalesPaginated.page}</span> of <span className="font-medium">{dailySalesPaginated.total_pages}</span> ({dailySalesPaginated.total} total)
              </div>
              <div className="flex gap-2">
                <button
                  disabled={!dailySalesPaginated.has_prev}
                  onClick={() => handleFilterChange('page', (filters.page || 1) - 1)}
                  className="px-3 py-1 border border-gray-300 rounded-md text-sm disabled:opacity-50 hover:bg-gray-50 flex items-center gap-1"
                >
                  <ChevronLeft size={16} /> Previous
                </button>
                <button
                  disabled={!dailySalesPaginated.has_next}
                  onClick={() => handleFilterChange('page', (filters.page || 1) + 1)}
                  className="px-3 py-1 border border-gray-300 rounded-md text-sm disabled:opacity-50 hover:bg-gray-50 flex items-center gap-1"
                >
                  Next <ChevronRight size={16} />
                </button>
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="bg-white p-8 rounded-lg text-center text-gray-500">No data available for selected period</div>
      )}
    </div>
  );

  const renderContractsTab = () => (
    <div className="space-y-6">
      <div className="bg-white p-4 rounded-lg border border-gray-200">
        <h3 className="font-semibold text-gray-900 mb-4">Contract Performance</h3>
        {contractLoading ? (
          <div className="text-center text-gray-500 py-8">Loading...</div>
        ) : contractData && contractData.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {contractData.map((contract, idx) => (
              <div key={idx} className="border border-gray-200 rounded-lg p-4">
                <h4 className="font-semibold text-gray-900 mb-2">{contract.contract_name || 'Contract'}</h4>
                <div className="space-y-1 text-sm text-gray-600">
                  <div>Revenue: <span className="font-semibold text-gray-900">₵{contract.revenue.toFixed(2)}</span></div>
                  <div>Discounts: <span className="font-semibold text-gray-900">₵{contract.discount_given.toFixed(2)}</span></div>
                  <div>Customers: <span className="font-semibold text-gray-900">{contract.customer_count}</span></div>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-center text-gray-500 py-8">No contracts found</div>
        )}
      </div>
    </div>
  );

  const renderInventoryTab = () => (
    <div className="space-y-6">
      <div className="bg-white p-4 rounded-lg border border-gray-200">
        <h3 className="font-semibold text-gray-900 mb-4">Inventory Alerts</h3>
        {inventoryLoading ? (
          <div className="text-center text-gray-500 py-8">Loading...</div>
        ) : inventoryData && inventoryData.length > 0 ? (
          <div className="space-y-2">
            {inventoryData.map((alert: any, idx: number) => (
              <div key={idx} className="flex items-center gap-3 p-3 bg-yellow-50 border border-yellow-200 rounded-lg">
                <AlertCircle size={20} className="text-yellow-600" />
                <div className="flex-1">
                  <p className="font-semibold text-gray-900">{alert.drug_name || 'Unknown Drug'}</p>
                  <p className="text-sm text-gray-600">{alert.alert_type} - {alert.message}</p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-center text-gray-500 py-8">No alerts</div>
        )}
      </div>
    </div>
  );

  const renderDrugTurnoverTab = () => (
    <div className="space-y-6">
      {/* Filters */}
      {showFilters && (
        <div className="bg-white p-4 rounded-lg border border-gray-200 space-y-4">
          <h3 className="font-semibold text-gray-900">Filters</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Start Date</label>
              <input
                type="date"
                value={filters.startDate}
                onChange={(e) => handleFilterChange('startDate', e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">End Date</label>
              <input
                type="date"
                value={filters.endDate}
                onChange={(e) => handleFilterChange('endDate', e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Branch</label>
              <select
                value={filters.branchId || ''}
                onChange={(e) => handleFilterChange('branchId', e.target.value || undefined)}
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm"
              >
                <option value="">All Branches</option>
              </select>
            </div>
          </div>
        </div>
      )}

      {/* Data Table */}
      {drugTurnoverLoading ? (
        <div className="bg-white p-8 rounded-lg text-center text-gray-500">Loading drug turnover data...</div>
      ) : drugTurnoverData && drugTurnoverData.length > 0 ? (
        <div className="space-y-4">
          <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
            <table className="w-full">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-700 uppercase">Drug</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-700 uppercase">Units Sold</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-700 uppercase">Revenue</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-700 uppercase">Category</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-700 uppercase">Avg Price</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200">
                {drugTurnoverData.map((row: any, idx: number) => (
                  <tr key={idx} className="hover:bg-gray-50">
                    <td className="px-6 py-3 text-sm font-medium text-gray-900">{row.drug_name || 'Unknown'}</td>
                    <td className="px-6 py-3 text-sm text-gray-900">{row.units_sold || 0}</td>
                    <td className="px-6 py-3 text-sm font-semibold text-gray-900">${parseFloat(row.revenue || 0).toFixed(2)}</td>
                    <td className="px-6 py-3 text-sm text-gray-600">{row.category ? row.category.toUpperCase() : '-'}</td>
                    <td className="px-6 py-3 text-sm text-gray-900">${parseFloat(row.avg_selling_price || 0).toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* Pagination Controls */}
          {drugTurnoverPaginated && drugTurnoverPaginated.total_pages > 1 && (
            <div className="flex items-center justify-between bg-white px-4 py-3 border border-gray-200 rounded-lg">
              <div className="text-sm text-gray-700">
                Showing page <span className="font-medium">{drugTurnoverPaginated.page}</span> of <span className="font-medium">{drugTurnoverPaginated.total_pages}</span> ({drugTurnoverPaginated.total} total)
              </div>
              <div className="flex gap-2">
                <button
                  disabled={!drugTurnoverPaginated.has_prev}
                  onClick={() => handleFilterChange('page', (filters.page || 1) - 1)}
                  className="px-3 py-1 border border-gray-300 rounded-md text-sm disabled:opacity-50 hover:bg-gray-50 flex items-center gap-1"
                >
                  <ChevronLeft size={16} /> Previous
                </button>
                <button
                  disabled={!drugTurnoverPaginated.has_next}
                  onClick={() => handleFilterChange('page', (filters.page || 1) + 1)}
                  className="px-3 py-1 border border-gray-300 rounded-md text-sm disabled:opacity-50 hover:bg-gray-50 flex items-center gap-1"
                >
                  Next <ChevronRight size={16} />
                </button>
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="bg-white p-8 rounded-lg text-center text-gray-500">No drug turnover data available for selected period</div>
      )}
    </div>
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <BarChart2 size={32} className="text-blue-600" />
          <div>
            <h1 className="text-3xl font-bold text-gray-900">Reports</h1>
            <p className="text-gray-600">Analytics and business intelligence</p>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="bg-white border-b border-gray-200">
        <div className="flex gap-8 px-6">
          {[
            { id: 'daily-sales', label: 'Daily Sales' },
            { id: 'contracts', label: 'Contracts' },
            { id: 'inventory', label: 'Inventory Alerts' },
            { id: 'drugs', label: 'Drug Turnover' },
          ].map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id as any)}
              className={`py-4 px-2 border-b-2 font-medium transition-colors ${
                activeTab === tab.id
                  ? 'border-green-600 text-green-600'
                  : 'border-transparent text-gray-600 hover:text-gray-900'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab Content */}
      <div className="space-y-6">
        {activeTab === 'daily-sales' && renderDailySalesTab()}
        {activeTab === 'contracts' && renderContractsTab()}
        {activeTab === 'inventory' && renderInventoryTab()}
        {activeTab === 'drugs' && renderDrugTurnoverTab()}
      </div>
    </div>
  );
}
