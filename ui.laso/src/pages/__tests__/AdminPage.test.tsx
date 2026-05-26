/** @vitest-environment jsdom */
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi, describe, it, expect } from 'vitest';

// Mock auth store to simulate different roles
vi.mock('@/stores/authStore', () => ({
  useAuthStore: (selector: any) => {
    const state = { user: { role: 'manager', full_name: 'Test Manager' } };
    return selector ? selector(state) : state;
  }
}));

import AdminPage from '../AdminPage';

describe('AdminPage access and rendering', () => {
  it('renders administration header for manager role', () => {
    render(
      <MemoryRouter>
        <AdminPage />
      </MemoryRouter>
    );

    expect(screen.getByText(/Administration/i)).toBeTruthy();
    expect(screen.getByText(/Manage Drugs/i)).toBeTruthy();
  });
});
