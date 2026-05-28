/**
 * DataFreshnessIndicator.tsx
 * ==========================
 * Visual badge component showing when data comes from cache instead of server.
 * Displays as a small badge in the page header or table header.
 */

import { AlertCircle, Clock } from "lucide-react";
import { useState, useEffect } from "react";

export interface DataFreshnessIndicatorProps {
    isFromCache: boolean;
    cached_at?: string;
    error?: string;
    compact?: boolean;  // Show minimal version for inline use
}

/**
 * Shows "Using cached data" badge when appropriate.
 * @param isFromCache - Whether data came from local cache
 * @param cached_at - Timestamp when cache was read
 * @param error - Error message if fetch failed
 * @param compact - If true, shows condensed version
 */
export function DataFreshnessIndicator({
    isFromCache,
    cached_at,
    error,
    compact = false,
}: DataFreshnessIndicatorProps) {
    const [relativeTime, setRelativeTime] = useState<string>("");

    useEffect(() => {
        if (!cached_at) return;

        const updateTime = () => {
            const age = Date.now() - new Date(cached_at).getTime();
            const seconds = Math.floor(age / 1000);
            const minutes = Math.floor(seconds / 60);
            const hours = Math.floor(minutes / 60);

            if (seconds < 60) {
                setRelativeTime("just now");
            } else if (minutes < 60) {
                setRelativeTime(`${minutes}m ago`);
            } else if (hours < 24) {
                setRelativeTime(`${hours}h ago`);
            } else {
                setRelativeTime(`${Math.floor(hours / 24)}d ago`);
            }
        };

        updateTime();
        const interval = setInterval(updateTime, 30000); // Update every 30s
        return () => clearInterval(interval);
    }, [cached_at]);

    if (!isFromCache) {
        return null; // Don't show anything for fresh data
    }

    if (compact) {
        return (
            <span
                className="inline-flex items-center gap-1 px-2 py-1 bg-amber-50 border border-amber-200 rounded text-xs font-medium text-amber-700 hover:bg-amber-100 transition-colors"
                title={error ? `Error: ${error}` : `Last synced: ${relativeTime}`}
            >
                <Clock className="w-3 h-3" />
                {relativeTime}
            </span>
        );
    }

    // Full badge version
    return (
        <div
            className={`flex items-start gap-3 p-3 rounded-lg border transition-colors ${
                error
                    ? "bg-red-50 border-red-200"
                    : "bg-amber-50 border-amber-200"
            }`}
        >
            <div className="flex-shrink-0 mt-0.5">
                <AlertCircle
                    className={`w-4 h-4 ${
                        error ? "text-red-600" : "text-amber-600"
                    }`}
                />
            </div>
            <div className="flex-1 min-w-0">
                <h3
                    className={`text-sm font-semibold ${
                        error ? "text-red-900" : "text-amber-900"
                    }`}
                >
                    {error ? "Connection Issue" : "Using Cached Data"}
                </h3>
                <p
                    className={`text-sm mt-0.5 ${
                        error ? "text-red-700" : "text-amber-700"
                    }`}
                >
                    {error
                        ? error
                        : `Last synced: ${relativeTime || "just now"}`}
                </p>
            </div>
        </div>
    );
}
