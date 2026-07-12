/** @vitest-environment jsdom */
import { describe, expect, it, vi } from 'vitest';
import { AxiosError } from 'axios';
import {
  BACKEND_CONNECTIVITY_EVENT,
  markBackendOffline,
  markBackendOnline,
  parseApiError,
} from '@/api/client';

function makeAxiosError(status: number, data: unknown): AxiosError {
  return new AxiosError(
    undefined,
    undefined,
    undefined,
    undefined,
    {
      status,
      data,
      headers: {},
      statusText: '',
      config: {} as any,
    },
  );
}

function makeNetworkError(code: string, message: string): AxiosError {
  const err = new AxiosError(message, code);
  return err;
}

function makeCancelError(): AxiosError {
  const err = new AxiosError('canceled', 'ERR_CANCELED');
  return err;
}

describe('parseApiError', () => {
  it('extracts detail string from Axios error response', () => {
    const err = makeAxiosError(400, {
      detail: 'Insufficient stock for Paracetamol. Available: 3, Requested: 10.',
    });
    expect(parseApiError(err)).toBe(
      'Insufficient stock for Paracetamol. Available: 3, Requested: 10.',
    );
  });

  it('does not expose server exception details from 5xx responses', () => {
    const err = makeAxiosError(500, {
      detail: 'ProgrammingError: SELECT users.password_hash FROM users',
    });
    const result = parseApiError(err);

    expect(result).toBe('Something went wrong on our side. Please try again.');
    expect(result).not.toContain('ProgrammingError');
    expect(result).not.toContain('SELECT');
  });

  it('extracts errors array from validation error', () => {
    const err = makeAxiosError(422, {
      errors: [
        { loc: ['body', 'quantity'], msg: 'Value must be positive', type: 'value_error' },
      ],
    });
    expect(parseApiError(err)).toBe('quantity: Value must be positive');
  });

  it('extracts validation error without loc', () => {
    const err = makeAxiosError(422, {
      errors: [
        { msg: 'Global validation error' },
      ],
    });
    expect(parseApiError(err)).toBe('Global validation error');
  });

  it('prefers errors array over detail string', () => {
    const err = makeAxiosError(422, {
      errors: [{ loc: ['body', 'name'], msg: 'Name is required' }],
      detail: 'Validation error',
    });
    expect(parseApiError(err)).toContain('Name is required');
  });

  it('extracts message field when detail is absent', () => {
    const err = makeAxiosError(400, { message: 'Operation timed out' });
    expect(parseApiError(err)).toBe('Operation timed out');
  });

  it('extracts error field as fallback', () => {
    const err = makeAxiosError(400, { error: 'Invalid token' });
    expect(parseApiError(err)).toBe('Invalid token');
  });

  it('returns network message for ERR_NETWORK', () => {
    const err = makeNetworkError('ERR_NETWORK', 'Network Error');
    expect(parseApiError(err)).toBe('Cannot connect to server. Make sure the backend is running.');
  });

  it('returns network message for ECONNREFUSED', () => {
    const err = makeNetworkError('ECONNREFUSED', 'connect ECONNREFUSED');
    expect(parseApiError(err)).toBe('Cannot connect to server. Make sure the backend is running.');
  });

  it('returns timeout message for ECONNABORTED', () => {
    const err = makeNetworkError('ECONNABORTED', 'timeout');
    expect(parseApiError(err)).toBe('Request timed out. Try again.');
  });

  it('returns empty string for cancelled requests', () => {
    const err = makeCancelError();
    expect(parseApiError(err)).toBe('');
  });

  it('returns generic Error message', () => {
    const err = new Error('Something broke');
    expect(parseApiError(err)).toBe('Something broke');
  });

  it('falls back to generic message for unknown error shapes', () => {
    expect(parseApiError('raw string')).toBe('An unexpected error occurred');
  });

  it('handles null/undefined', () => {
    expect(parseApiError(null)).toBe('An unexpected error occurred');
    expect(parseApiError(undefined)).toBe('An unexpected error occurred');
  });

  it('handles FastAPI validation error array in detail', () => {
    const err = makeAxiosError(422, {
      detail: [
        { msg: 'field1: must be a valid email' },
        { msg: 'field2: must be at least 8 characters' },
      ],
    });
    const result = parseApiError(err);
    expect(result).toContain('field1: must be a valid email');
    expect(result).toContain('field2: must be at least 8 characters');
  });

  it('joins multiple validation errors', () => {
    const err = makeAxiosError(422, {
      errors: [
        { loc: ['body', 'email'], msg: 'Invalid email format', type: 'value_error' },
        { loc: ['body', 'password'], msg: 'Must be at least 8 characters', type: 'value_error' },
      ],
    });
    const result = parseApiError(err);
    expect(result).toContain('email');
    expect(result).toContain('password');
    expect(result).toContain('Invalid email format');
    expect(result).toContain('Must be at least 8 characters');
  });

  it('filters out null items from errors array', () => {
    const err = makeAxiosError(422, {
      errors: [
        null,
        { loc: ['body', 'name'], msg: 'Name is required' },
      ],
    });
    expect(parseApiError(err)).toBe('name: Name is required');
  });
});

describe('error message quality', () => {
  it('inventory errors include drug name and quantities', () => {
    const msg = "Insufficient stock for 'Paracetamol'. Available: 3, Requested: 10.";
    expect(msg).toContain('Paracetamol');
    expect(msg).toContain('Available: 3');
    expect(msg).toContain('Requested: 10');
  });

  it('adjustment errors include current and requested values', () => {
    const msg = 'Adjustment would result in negative stock. Current: 5, Change: -10, Result: -5.';
    expect(msg).toContain('Current: 5');
    expect(msg).toContain('Change: -10');
  });

  it('validation errors include field name and constraint', () => {
    const msg = 'Expiry date must be in the future';
    expect(msg).toContain('Expiry date');
    expect(msg).toContain('future');
  });

  it('duplicate errors include the conflicting value', () => {
    const msg = "Prescription number 'RX-001' already exists";
    expect(msg).toContain('RX-001');
    expect(msg).toContain('already exists');
  });
});

describe('backend connectivity events', () => {
  it('broadcasts offline and online transitions for sync status consumers', () => {
    const listener = vi.fn();
    window.addEventListener(BACKEND_CONNECTIVITY_EVENT, listener);

    markBackendOnline();
    markBackendOffline();
    markBackendOffline();
    markBackendOnline();

    window.removeEventListener(BACKEND_CONNECTIVITY_EVENT, listener);

    expect(listener).toHaveBeenCalledTimes(2);
    expect(listener.mock.calls[0][0].detail.reachable).toBe(false);
    expect(listener.mock.calls[0][0].detail.offlineSince).toEqual(expect.any(Number));
    expect(listener.mock.calls[1][0].detail).toEqual({
      reachable: true,
      offlineSince: null,
    });
  });
});
