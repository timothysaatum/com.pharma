/**
 * Admin Page Component
 * Consolidated tab interface for Drugs, Inventory, Purchase Orders, and Contracts
 * Renders the full existing page components in a tabbed layout
 */

import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { AlertCircle } from 'lucide-react';
import { useAuthStore } from '@/stores/authStore';
import { ADMIN_TABS, parseAdminTab, type AdminTabId } from '@/lib/routes';
import DrugListPage from './DrugListPage';
import InventoryPage from './InventoryPage';
import PurchasesPage from './PurchasesPage';
import ContractsPage from './ContractsPage';

export default function AdminPage() {
  const user = useAuthStore((state) => state.user);
  const navigate = useNavigate();
  const { tab } = useParams();
  const [activeTab, setActiveTab] = useState<AdminTabId>(() => parseAdminTab(tab));

  useEffect(() => {
    const nextTab = parseAdminTab(tab);
    setActiveTab(nextTab);
    if (tab && tab !== nextTab) {
      navigate(`/admin/${nextTab}`, { replace: true });
    }
  }, [navigate, tab]);

  // Check authorization - allow managers to access consolidated admin view
  if (!user || !['admin', 'super_admin', 'manager'].includes(user.role)) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="bg-white p-8 rounded-lg shadow-lg max-w-md text-center">
          <div className="flex justify-center mb-4">
            <AlertCircle size={48} className="text-red-600" />
          </div>
          <h1 className="text-2xl font-bold text-gray-900 mb-2">Access Denied</h1>
          <p className="text-gray-600">You don't have permission to access the admin panel.</p>
        </div>
      </div>
    );
  }

  const labels: Record<AdminTabId, string> = {
    drugs: 'Drugs',
    inventory: 'Inventory',
    purchases: 'Purchases',
    contracts: 'Contracts',
  };

  const selectTab = (nextTab: AdminTabId) => {
    setActiveTab(nextTab);
    navigate(nextTab === 'drugs' ? '/admin' : `/admin/${nextTab}`);
  };

  return (
    <div className="flex flex-col h-full min-h-0 bg-surface">
      {/* Tabs */}
      <div className="flex gap-2 border-b border-gray-200 px-4 bg-white flex-shrink-0">
        {ADMIN_TABS.map(tab => (
          <button
            key={tab}
            onClick={() => selectTab(tab)}
            className={`py-2 px-4 font-medium transition-colors whitespace-nowrap ${
              activeTab === tab
                ? 'border-b-2 border-blue-600 text-blue-600'
                : 'text-gray-600 hover:text-gray-900'
            }`}
          >
            {labels[tab]}
          </button>
        ))}
      </div>

      {/* Tab Content - Render full existing components */}
      <div className="flex-1 min-h-0 overflow-auto">
        {activeTab === 'drugs' && <DrugListPage />}
        {activeTab === 'inventory' && <InventoryPage />}
        {activeTab === 'purchases' && <PurchasesPage />}
        {activeTab === 'contracts' && <ContractsPage />}
      </div>
    </div>
  );
}
