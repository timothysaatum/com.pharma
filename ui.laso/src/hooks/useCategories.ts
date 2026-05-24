import { useState, useEffect, useRef } from "react";
import { drugApi } from "@/api/drugs";
import type { DrugCategory, DrugCategoryTree } from "@/types";

// ─────────────────────────────────────────────────────────────────────────────
// Module-level caches shared across all component instances.
// Resets on module reload (dev HMR / page refresh) which is intentional.
// ─────────────────────────────────────────────────────────────────────────────

let flatCache: DrugCategory[] | null = null;
let flatInflight: Promise<DrugCategory[]> | null = null;

let treeCache: DrugCategoryTree[] | null = null;
let treeInflight: Promise<DrugCategoryTree[]> | null = null;

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
    const [categories, setCategories] = useState<DrugCategory[]>(flatCache ?? []);
    const [isLoading, setIsLoading] = useState(flatCache === null);
    const [error, setError] = useState<string | null>(null);
    const mounted = useRef(true);

    useEffect(() => {
        mounted.current = true;

        if (flatCache !== null) {
            setCategories(flatCache);
            setIsLoading(false);
            return;
        }

        if (!flatInflight) {
            flatInflight = drugApi.listCategories();
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
    }, []);

    /** Force-refresh (e.g. after creating a new category). */
    function invalidate() {
        flatCache = null;
        flatInflight = null;
        setIsLoading(true);
        setError(null);
        drugApi
            .listCategories()
            .then((data) => {
                flatCache = data;
                if (mounted.current) {
                    setCategories(data);
                    setIsLoading(false);
                }
            })
            .catch((err) => {
                if (mounted.current) {
                    setError(err?.message ?? "Failed to load categories");
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
    const [tree, setTree] = useState<DrugCategoryTree[]>(treeCache ?? []);
    const [isLoading, setIsLoading] = useState(treeCache === null);
    const [error, setError] = useState<string | null>(null);
    const mounted = useRef(true);

    useEffect(() => {
        mounted.current = true;

        if (treeCache !== null) {
            setTree(treeCache);
            setIsLoading(false);
            return;
        }

        if (!treeInflight) {
            treeInflight = drugApi.listCategoriesTree();
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
                    setError(err?.message ?? "Failed to load category tree");
                    setIsLoading(false);
                }
            });

        return () => { mounted.current = false; };
    }, []);

    /** Force-refresh both caches so flat and tree stay in sync. */
    function invalidate() {
        flatCache = null;
        flatInflight = null;
        treeCache = null;
        treeInflight = null;
        setIsLoading(true);
        setError(null);
        drugApi
            .listCategoriesTree()
            .then((data) => {
                treeCache = data;
                if (mounted.current) {
                    setTree(data);
                    setIsLoading(false);
                }
            })
            .catch((err) => {
                if (mounted.current) {
                    setError(err?.message ?? "Failed to load category tree");
                    setIsLoading(false);
                }
            });
    }

    return { tree, isLoading, error, invalidate };
}