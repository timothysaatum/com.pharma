import { useState, useEffect } from "react";

/**
 * Delays updating the returned value until the input hasn't changed
 * for `delay` milliseconds. Use for search inputs to avoid firing
 * an API request on every keystroke.
 */
export function useDebounce<T>(value: T, delay = 300): T {
    const [debounced, setDebounced] = useState<T>(value);

    useEffect(() => {
        const timer = setTimeout(() => setDebounced(value), delay);
        return () => clearTimeout(timer);
    }, [value, delay]);

    return debounced;
}