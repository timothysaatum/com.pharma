/**
 * App-wide event bus for instant UI updates.
 *
 * When any mutation succeeds, emit the relevant event.
 * Any hook or page that holds that data listens and re-fetches immediately.
 *
 * Usage:
 *   // After a successful create/update/delete:
 *   appEvents.emit("drugs:changed")
 *
 *   // In a hook that displays that data:
 *   useAppEvent("drugs:changed", fetchDrugs)
 */

type AppEventType =
    | "drugs:changed"       // drug created / updated / deleted / deactivated
    | "inventory:changed"   // batch added, stock adjusted, transfer done
    | "sales:changed"       // sale completed / voided
    | "purchases:changed"   // PO created / received / updated
    | "customers:changed";  // customer created / updated

type Listener = () => void;

class AppEventBus {
    private listeners = new Map<AppEventType, Set<Listener>>();

    on(event: AppEventType, listener: Listener): () => void {
        if (!this.listeners.has(event)) {
            this.listeners.set(event, new Set());
        }
        this.listeners.get(event)!.add(listener);
        // Return unsubscribe function
        return () => this.listeners.get(event)?.delete(listener);
    }

    emit(event: AppEventType): void {
        this.listeners.get(event)?.forEach((fn) => fn());
    }
}

export const appEvents = new AppEventBus();

/**
 * React hook — subscribe to an app event and re-run a callback when it fires.
 * Automatically unsubscribes on unmount.
 *
 * @example
 * useAppEvent("drugs:changed", fetchDrugs)
 */
import { useEffect } from "react";

export function useAppEvent(event: AppEventType, callback: Listener): void {
    useEffect(() => {
        return appEvents.on(event, callback);
    }, [event, callback]);
}