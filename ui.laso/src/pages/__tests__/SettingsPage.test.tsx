/** @vitest-environment jsdom */
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@/components/settings/OrganizationTab', () => ({
  OrganizationTab: () => <div>Organization settings tab</div>,
}));

vi.mock('@/components/settings/BranchesTab', () => ({
  BranchesTab: () => <div>Branches settings tab</div>,
}));

import SettingsPage from '../SettingsPage';

describe('SettingsPage tabs', () => {
  it('renders organization settings by default', () => {
    render(
      <MemoryRouter initialEntries={['/settings']}>
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </MemoryRouter>
    );

    expect(screen.getByText('Organization settings tab')).toBeTruthy();
  });

  it('selects branch management from the route', () => {
    render(
      <MemoryRouter initialEntries={['/settings/branches']}>
        <Routes>
          <Route path="/settings/:tab" element={<SettingsPage />} />
        </Routes>
      </MemoryRouter>
    );

    expect(screen.getByText('Branches settings tab')).toBeTruthy();
  });
});
