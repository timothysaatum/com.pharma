import { useState, useEffect, useRef } from "react";
import { drugApi } from "@/api/drugs";
import { localRead } from "@/lib/localRead";
import { isBackendKnownUnreachable, isOfflineError, parseApiError } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import type { DrugCategory, DrugCategoryTree } from "@/types";

// ─────────────────────────────────────────────────────────────────────────────
// Module-level caches shared across all component instances.
// Resets on module reload (dev HMR / page refresh) which is intentional.
// ─────────────────────────────────────────────────────────────────────────────

let flatCache: DrugCategory[] | null = null;
let flatInflight: Promise<DrugCategory[]> | null = null;

let treeCache: DrugCategoryTree[] | null = null;
let treeInflight: Promise<DrugCategoryTree[]> | null = null;
let categoryCacheOrganizationId: string | null = null;

function scopeCategoryCaches(organizationId: string | null): void {
    if (categoryCacheOrganizationId === organizationId) return;
    categoryCacheOrganizationId = organizationId;
    flatCache = null;
    flatInflight = null;
    treeCache = null;
    treeInflight = null;
}

function shouldReadCategoriesFromLocal(): boolean {
    if (typeof navigator === "undefined") return false;
    return !navigator.onLine || isBackendKnownUnreachable();
}

function readLocalCategories(organizationId: string | null): Promise<DrugCategory[]> {
    return localRead.getDrugCategories(undefined, organizationId ?? undefined);
}

function readLocalCategoryTree(organizationId: string | null): Promise<DrugCategoryTree[]> {
    return localRead.getDrugCategoryTree(organizationId ?? undefined);
}

async function loadFlatCategories(organizationId: string | null): Promise<DrugCategory[]> {
    if (shouldReadCategoriesFromLocal()) {
        return readLocalCategories(organizationId);
    }

    try {
        const remote = await drugApi.listCategories();
        if (remote.length > 0) return remote;

        const local = await readLocalCategories(organizationId).catch(() => [] as DrugCategory[]);
        return local.length > 0 ? local : remote;
    } catch (err) {
        if (isOfflineError(err)) {
            return readLocalCategories(organizationId);
        }
        throw err;
    }
}

async function loadCategoryTree(organizationId: string | null): Promise<DrugCategoryTree[]> {
    if (shouldReadCategoriesFromLocal()) {
        return readLocalCategoryTree(organizationId);
    }

    try {
        const remote = await drugApi.listCategoriesTree();
        if (remote.length > 0) return remote;

        const local = await readLocalCategoryTree(organizationId).catch(() => [] as DrugCategoryTree[]);
        return local.length > 0 ? local : remote;
    } catch (err) {
        if (isOfflineError(err)) {
            return readLocalCategoryTree(organizationId);
        }
        throw err;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// useCategories — flat list
//
// Returns DrugCategory[] (no children).
// Use for dropdowns that only need id/name, or for filtering by parent_id.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Fetches the flat drug category list once per app session.
 * Any number of components can call this hook — only one network
 * request is ever made and all instances share the result.
 *
 * For a nested tree (category picker with children), use `useCategoryTree`.
 */
export function useCategories() {
    const organizationId = useAuthStore((state) => state.user?.organization_id ?? null);
    scopeCategoryCaches(organizationId);
    const [categories, setCategories] = useState<DrugCategory[]>(flatCache ?? []);
    const [isLoading, setIsLoading] = useState(flatCache === null);
    const [error, setError] = useState<string | null>(null);
    const mounted = useRef(true);

    useEffect(() => {
        mounted.current = true;
        const preferLocal = shouldReadCategoriesFromLocal();

        if (flatCache !== null && !preferLocal) {
            setCategories(flatCache);
            setIsLoading(false);
            return;
        }
        setCategories(flatCache ?? []);
        setIsLoading(true);
        setError(null);

        if (!flatInflight || preferLocal) {
            flatInflight = loadFlatCategories(organizationId);
        }

        flatInflight
            .then((data) => {
                flatCache = data;
                flatInflight = null;
                if (mounted.current) {
                    setCategories(data);
                    setIsLoading(false);
                }
            })
            .catch((err) => {
                flatInflight = null;
                if (mounted.current) {
                    setError(err?.message ?? "Failed to load categories");
                    setIsLoading(false);
                }
            });

        return () => { mounted.current = false; };
    }, [organizationId]);

    /** Force-refresh (e.g. after creating a new category). */
    function invalidate() {
        flatCache = null;
        flatInflight = null;
        setIsLoading(true);
        setError(null);

        loadFlatCategories(organizationId)
            .then((data) => {
                flatCache = data;
                if (mounted.current) {
                    setCategories(data);
                    setIsLoading(false);
                }
            })
            .catch((err) => {
                if (isOfflineError(err)) {
                    localRead.getDrugCategories(
                        undefined,
                        organizationId ?? undefined
                    )
                        .then((data) => {
                            flatCache = data;
                            if (mounted.current) {
                                setCategories(data);
                                setIsLoading(false);
                            }
                        })
                        .catch(() => {
                            if (mounted.current) {
                                setError(parseApiError(err));
                                setIsLoading(false);
                            }
                        });
                    return;
                }
                if (mounted.current) {
                    setError(parseApiError(err));
                    setIsLoading(false);
                }
            });
    }

    return { categories, isLoading, error, invalidate };
}

// ─────────────────────────────────────────────────────────────────────────────
// useCategoryTree — nested tree
//
// Returns DrugCategoryTree[] where each node has a `children` array.
// Use for category picker UIs that need to display hierarchy.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Fetches the full nested category tree once per app session.
 * Calls GET /drugs/categories/tree (not /drugs/categories).
 */
export function useCategoryTree() {
    const organizationId = useAuthStore((state) => state.user?.organization_id ?? null);
    scopeCategoryCaches(organizationId);
    const [tree, setTree] = useState<DrugCategoryTree[]>(treeCache ?? []);
    const [isLoading, setIsLoading] = useState(treeCache === null);
    const [error, setError] = useState<string | null>(null);
    const mounted = useRef(true);

    useEffect(() => {
        mounted.current = true;
        const preferLocal = shouldReadCategoriesFromLocal();

        if (treeCache !== null && !preferLocal) {
            setTree(treeCache);
            setIsLoading(false);
            return;
        }
        setTree(treeCache ?? []);
        setIsLoading(true);
        setError(null);

        if (!treeInflight || preferLocal) {
            treeInflight = loadCategoryTree(organizationId);
        }

        treeInflight
            .then((data) => {
                treeCache = data;
                treeInflight = null;
                if (mounted.current) {
                    setTree(data);
                    setIsLoading(false);
                }
            })
            .catch((err) => {
                treeInflight = null;
                if (mounted.current) {
                    setError(parseApiError(err));
                    setIsLoading(false);
                }
            });

        return () => { mounted.current = false; };
    }, [organizationId]);

    /** Force-refresh both caches so flat and tree stay in sync. */
    function invalidate() {
        flatCache = null;
        flatInflight = null;
        treeCache = null;
        treeInflight = null;
        setIsLoading(true);
        setError(null);

        loadCategoryTree(organizationId)
            .then((data) => {
                treeCache = data;
                if (mounted.current) {
                    setTree(data);
                    setIsLoading(false);
                }
            })
            .catch((err) => {
                if (isOfflineError(err)) {
                    localRead.getDrugCategoryTree(
                        organizationId ?? undefined
                    )
                        .then((data) => {
                            treeCache = data;
                            if (mounted.current) {
                                setTree(data);
                                setIsLoading(false);
                            }
                        })
                        .catch(() => {
                            if (mounted.current) {
                                setError(parseApiError(err));
                                setIsLoading(false);
                            }
                        });
                    return;
                }
                if (mounted.current) {
                    setError(parseApiError(err));
                    setIsLoading(false);
                }
            });
        }

    return { tree, isLoading, error, invalidate };
}
