/** @vitest-environment jsdom */
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi, describe, it, expect } from 'vitest';

vi.mock('../DrugListPage', () => ({ default: () => <div>Drug catalogue tab</div> }));
vi.mock('../InventoryPage', () => ({ default: () => <div>Inventory tab</div> }));
vi.mock('../PurchasesPage', () => ({ default: () => <div>Purchases tab</div> }));
vi.mock('../ContractsPage', () => ({ default: () => <div>Contracts tab</div> }));
vi.mock('../PrescriptionsPage', () => ({ default: () => <div>Prescriptions tab</div> }));

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

  // P3: the Prescriptions tab used to navigate to /prescriptions, silently
  // leaving Admin. These assert it renders IN PLACE and does not navigate away.
  it('renders the prescriptions tab in place from the /admin/prescriptions route', () => {
    render(
      <MemoryRouter initialEntries={['/admin/prescriptions']}>
        <Routes>
          <Route path="/admin/:tab" element={<AdminPage />} />
          {/* If the redirect were still in place this would render instead. */}
          <Route path="/prescriptions" element={<div>LEFT ADMIN</div>} />
        </Routes>
      </MemoryRouter>
    );

    expect(screen.getByText('Prescriptions tab')).toBeTruthy();
    expect(screen.queryByText('LEFT ADMIN')).toBeNull();
  });

  it('clicking the Prescriptions tab does not navigate away from Admin', () => {
    render(
      <MemoryRouter initialEntries={['/admin']}>
        <Routes>
          <Route path="/" element={<div>ADMIN ROOT</div>} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="/admin/:tab" element={<AdminPage />} />
          <Route path="/prescriptions" element={<div>LEFT ADMIN</div>} />
        </Routes>
      </MemoryRouter>
    );

    expect(screen.getByText('Drug catalogue tab')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Prescriptions' }));

    expect(screen.getByText('Prescriptions tab')).toBeTruthy();
    // The bug: this used to appear here.
    expect(screen.queryByText('LEFT ADMIN')).toBeNull();
    // Still inside the Admin tab shell, not a standalone page.
    expect(screen.getByRole('button', { name: 'Contracts' })).toBeTruthy();
  });

  it('the other tabs still work after the change', () => {
    render(
      <MemoryRouter initialEntries={['/admin']}>
        <Routes>
          <Route path="/admin" element={<AdminPage />} />
          <Route path="/admin/:tab" element={<AdminPage />} />
        </Routes>
      </MemoryRouter>
    );

    fireEvent.click(screen.getByRole('button', { name: 'Contracts' }));
    expect(screen.getByText('Contracts tab')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Inventory' }));
    expect(screen.getByText('Inventory tab')).toBeTruthy();
  });
});
