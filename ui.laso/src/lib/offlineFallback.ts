export interface OfflineSalePathInput {
  browserOnline: boolean;
  appOffline: boolean;
  backendReachable: boolean;
}

export interface OfflineSaleErrorFallbackInput extends OfflineSalePathInput {
  offlineError: boolean;
  backendReachableBeforeAttempt: boolean;
}

export function shouldUseOfflineSalePath({
  browserOnline,
  appOffline,
  backendReachable,
}: OfflineSalePathInput): boolean {
  return !browserOnline || appOffline || !backendReachable;
}

export function shouldFallbackToOfflineSaleAfterError({
  offlineError,
  browserOnline,
  appOffline,
  backendReachable,
  backendReachableBeforeAttempt,
}: OfflineSaleErrorFallbackInput): boolean {
  if (!offlineError) return false;
  return shouldUseOfflineSalePath({
    browserOnline,
    appOffline,
    backendReachable,
  }) || !backendReachableBeforeAttempt;
}
