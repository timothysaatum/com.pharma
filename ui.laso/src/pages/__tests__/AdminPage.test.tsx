/** @vitest-environment jsdom */
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi, describe, it, expect } from 'vitest';

vi.mock('../DrugListPage', () => ({ default: () => <div>Drug catalogue tab</div> }));
vi.mock('../InventoryPage', () => ({ default: () => <div>Inventory tab</div> }));
vi.mock('../PurchasesPage', () => ({ default: () => <div>Purchases tab</div> }));
vi.mock('../ContractsPage', () => ({ default: () => <div>Contracts tab</div> }));

// Mock auth store to simulate different roles
vi.mock('@/stores/authStore', () => ({
  useAuthStore: (selector: any) => {
    const state = { user: { role: 'manager', full_name: 'Test Manager' } };
    return selector ? selector(state) : state;
  }
}));

import AdminPage from '../AdminPage';

describe('AdminPage access and rendering', () => {
  it('renders the default drugs tab for manager role', () => {
    render(
      <MemoryRouter>
        <AdminPage />
      </MemoryRouter>
    );

    expect(screen.getByRole('button', { name: 'Drugs' })).toBeTruthy();
    expect(screen.getByText('Drug catalogue tab')).toBeTruthy();
  });

  it('selects the inventory tab from the current route', () => {
    render(
      <MemoryRouter initialEntries={['/admin/inventory']}>
        <Routes>
          <Route path="/admin/:tab" element={<AdminPage />} />
        </Routes>
      </MemoryRouter>
    );

    expect(screen.getByText('Inventory tab')).toBeTruthy();
  });
});
