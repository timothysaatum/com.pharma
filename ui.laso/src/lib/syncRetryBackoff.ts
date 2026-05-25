/**
 * syncRetryBackoff.ts
 * ==================
 * Exponential backoff with jitter for sync retry logic.
 * Prevents retry storms and server strain from thundering herd.
 *
 * Strategy:
 *   - First retry: 1s
 *   - Second: 2s
 *   - Third: 4s
 *   - ...up to 60s (1 minute)
 *   - Jitter: ±0-1s per attempt (random variation to desynchronize retries)
 *
 * Usage:
 *   const backoff = new RetryBackoff();
 *   const delayMs = backoff.getDelay(attemptNumber);
 *   await new Promise(r => setTimeout(r, delayMs));
 */

export class RetryBackoff {
    private readonly baseDelays = [
        1_000,      // Attempt 0: 1s
        2_000,      // Attempt 1: 2s
        4_000,      // Attempt 2: 4s
        8_000,      // Attempt 3: 8s
        16_000,     // Attempt 4: 16s
        32_000,     // Attempt 5: 32s
        60_000,     // Attempt 6+: 60s (max)
    ];

    private readonly jitterMs = 1_000; // ±1 second jitter

    /**
     * Get the delay in milliseconds for the given attempt number (0-indexed).
     * Includes random jitter to prevent synchronized retries.
     *
     * @param attempt - Attempt number (0-based)
     * @returns Delay in milliseconds
     */
    getDelay(attempt: number): number {
        const baseDelay = this.baseDelays[
            Math.min(attempt, this.baseDelays.length - 1)
        ];

        // Random jitter: ±half of jitterMs
        const jitter = (Math.random() - 0.5) * this.jitterMs;
        const totalDelay = Math.max(0, baseDelay + jitter);

        return totalDelay;
    }

    /**
     * Check if we should still retry based on attempt count.
     * Useful to implement a maximum retry limit.
     *
     * @param attempt - Attempt number (0-based)
     * @param maxAttempts - Maximum number of attempts (default: unlimited)
     * @returns true if should retry
     */
    shouldRetry(attempt: number, maxAttempts: number = Infinity): boolean {
        return attempt < maxAttempts;
    }

    /**
     * Format delay for logging/UI display.
     * @param delayMs - Delay in milliseconds
     * @returns Formatted string (e.g., "2.5s")
     */
    formatDelay(delayMs: number): string {
        const seconds = (delayMs / 1000).toFixed(1);
        return `${seconds}s`;
    }
}

/**
 * Helper to wait with backoff.
 * @param attempt - Attempt number (0-based)
 * @returns Promise that resolves after backoff delay
 */
export async function waitWithBackoff(attempt: number): Promise<void> {
    const backoff = new RetryBackoff();
    const delayMs = backoff.getDelay(attempt);
    return new Promise((resolve) => setTimeout(resolve, delayMs));
}

/**
 * Execute an async function with exponential backoff retry.
 * @param fn - Async function to execute
 * @param maxAttempts - Maximum number of attempts (default: 7)
 * @param onRetry - Optional callback on retry (attempt, delayMs, error)
 * @returns Result of successful function call or throws on max attempts
 */
export async function executeWithBackoff<T>(
    fn: () => Promise<T>,
    maxAttempts: number = 7,
    onRetry?: (attempt: number, delayMs: number, error: unknown) => void
): Promise<T> {
    const backoff = new RetryBackoff();
    let lastError: unknown;

    for (let attempt = 0; attempt < maxAttempts; attempt++) {
        try {
            return await fn();
        } catch (err) {
            lastError = err;

            // Don't retry on last attempt
            if (attempt >= maxAttempts - 1) break;

            const delayMs = backoff.getDelay(attempt);
            onRetry?.(attempt, delayMs, err);

            await new Promise((resolve) => setTimeout(resolve, delayMs));
        }
    }

    throw lastError;
}
