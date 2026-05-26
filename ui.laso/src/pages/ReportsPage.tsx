/**
 * Reports Page Component
 * Comprehensive analytics and reporting interface
 * Supports: Daily Sales, Contracts, Inventory Alerts, Customers, Drug Turnover
 */

import { useState, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { BarChart2, Download, RefreshCw, AlertCircle } from 'lucide-react';
import { format, subDays } from 'date-fns';
import { reportsApi } from '../api/reports';

interface FilterState {
  startDate: string;
  endDate: string;
  branchId?: string;
  contractId?: string;
  cashierId?: string;
  limit?: number;
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
  });

  const [showFilters] = useState(true);

  // Daily Sales Query
  const { data: dailySalesData, isLoading: dailySalesLoading, refetch: refetchDailySales } = useQuery<DailySalesRow[]>({
    queryKey: ['reports', 'daily-sales', filters],
    queryFn: () => reportsApi.getDailySalesSummary({
      startDate: filters.startDate,
      endDate: filters.endDate,
      branchId: filters.branchId,
      contractId: filters.contractId,
      cashierId: filters.cashierId,
    }),
    enabled: activeTab === 'daily-sales',
  });

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
  const { data: drugTurnoverData, isLoading: drugTurnoverLoading } = useQuery<any[]>({
    queryKey: ['reports', 'drug-turnover', filters],
    queryFn: () => reportsApi.getDrugTurnover({
      startDate: filters.startDate,
      endDate: filters.endDate,
      branchId: filters.branchId,
      limit: 50,
    }),
    enabled: activeTab === 'drugs',
  });

  const handleFilterChange = useCallback((key: keyof FilterState, value: any) => {
    setFilters(prev => ({
      ...prev,
      [key]: value,
    }));
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
                {/* Add branch options from API */}
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
                {/* Add contract options from API */}
              </select>
            </div>
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => refetchDailySales()}
              className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 flex items-center gap-2"
            >
              <RefreshCw size={16} />
              Refresh
            </button>
            <button
              onClick={() => dailySalesData && exportToCSV(dailySalesData, 'daily-sales')}
              className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 flex items-center gap-2"
            >
              <Download size={16} />
              Export CSV
            </button>
          </div>
        </div>
      )}

      {/* Data Table */}
      {dailySalesLoading ? (
        <div className="bg-white p-8 rounded-lg text-center text-gray-500">Loading sales data...</div>
      ) : dailySalesData && dailySalesData.length > 0 ? (
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
                  <td className="px-6 py-3 text-sm font-semibold text-gray-900">${parseFloat((row.net_revenue ?? row.gross_revenue) || 0).toFixed(2)}</td>
                  <td className="px-6 py-3 text-sm text-gray-900">{row.total_items || 0}</td>
                  <td className="px-6 py-3 text-sm text-gray-900">{row.transaction_count || 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
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
                  <div>Revenue: <span className="font-semibold text-gray-900">${contract.revenue.toFixed(2)}</span></div>
                  <div>Discounts: <span className="font-semibold text-gray-900">${contract.discount_given.toFixed(2)}</span></div>
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
