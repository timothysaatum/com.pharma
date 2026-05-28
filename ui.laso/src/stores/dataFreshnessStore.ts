/**
 * dataFreshnessStore.ts
 * =====================
 * Zustand store to track which data came from server vs local cache.
 * Used to show visual indicators ("Using cached data") in the UI.
 */

import { create } from "zustand";

export interface DataFreshnessInfo {
    isFromCache: boolean;
    fetched_at?: string;
    cached_at?: string;
    error?: string;
}

export interface DataFreshnessState {
    freshData: Record<string, DataFreshnessInfo>;
    setFreshness: (key: string, info: DataFreshnessInfo) => void;
    clearFreshness: (key: string) => void;
    getFreshness: (key: string) => DataFreshnessInfo | null;
}

export const dataFreshnessStore = create<DataFreshnessState>((set, get) => ({
    freshData: {},

    setFreshness: (key: string, info: DataFreshnessInfo) => {
        set((state) => ({
            freshData: {
                ...state.freshData,
                [key]: info,
            },
        }));
    },

    clearFreshness: (key: string) => {
        set((state) => {
            const { [key]: _, ...rest } = state.freshData;
            return { freshData: rest };
        });
    },

    getFreshness: (key: string) => {
        return get().freshData[key] ?? null;
    },
}));
