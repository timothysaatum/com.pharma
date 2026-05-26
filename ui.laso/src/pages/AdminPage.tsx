/**
 * Admin Page Component
 * Consolidated tab interface for Drugs, Inventory, Purchase Orders, and Contracts
 * Renders the full existing page components in a tabbed layout
 */

import { useState } from 'react';
import { AlertCircle } from 'lucide-react';
import { useAuthStore } from '@/stores/authStore';
import DrugListPage from './DrugListPage';
import InventoryPage from './InventoryPage';
import PurchasesPage from './PurchasesPage';
import ContractsPage from './ContractsPage';

export default function AdminPage() {
  const user = useAuthStore((state) => state.user);
  const [activeTab, setActiveTab] = useState<'drugs' | 'inventory' | 'purchases' | 'contracts'>('drugs');

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

  const tabs = [
    { id: 'drugs', label: 'Drugs' },
    { id: 'inventory', label: 'Inventory' },
    { id: 'purchases', label: 'Purchases' },
    { id: 'contracts', label: 'Contracts' },
  ];

  return (
    <div className="space-y-4">
      {/* Tabs */}
      <div className="flex gap-2 border-b border-gray-200 px-4">
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id as any)}
            className={`py-2 px-4 font-medium transition-colors whitespace-nowrap ${
              activeTab === tab.id
                ? 'border-b-2 border-blue-600 text-blue-600'
                : 'text-gray-600 hover:text-gray-900'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab Content - Render full existing components */}
      <div>
        {activeTab === 'drugs' && <DrugListPage />}
        {activeTab === 'inventory' && <InventoryPage />}
        {activeTab === 'purchases' && <PurchasesPage />}
        {activeTab === 'contracts' && <ContractsPage />}
      </div>
    </div>
  );
}
