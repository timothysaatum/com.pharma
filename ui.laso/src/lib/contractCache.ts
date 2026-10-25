import { getDb } from "@/lib/localDb";
import { branchApi } from "@/api/branches";
import { contractsApi, type AvailableContract } from "@/api/contracts";
import {
  getPriceContractCacheInfo,
  replacePriceContractAvailability,
  type BranchAvailability,
} from "@/lib/priceContractStore";

const CACHE_TTL_MS = 5 * 60 * 1000;
const FETCH_CONCURRENCY = 4;

export interface ContractCacheContext {
  userId: string;
  organizationId: string;
  assignedBranches?: string[] | null;
  activeBranchId?: string | null;
  force?: boolean;
}

export interface ContractCacheResult {
  ok: boolean;
  skipped: boolean;
  branchCount: number;
  contractCount: number;
  error?: string;
}

const inflight = new Map<string, Promise<ContractCacheResult>>();

function errorStatus(error: unknown): number | null {
  const candidate = error as {
    status?: unknown;
    response?: { status?: unknown };
  } | null;
  const status = candidate?.response?.status ?? candidate?.status;
  return typeof status === "number" ? status : null;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

async function resolveBranchIds(context: ContractCacheContext): Promise<string[]> {
  const branchIds = new Set<string>();
  for (const branchId of context.assignedBranches ?? []) {
    if (branchId) branchIds.add(String(branchId));
  }
  if (context.activeBranchId) branchIds.add(String(context.activeBranchId));

  if (branchIds.size > 0) return [...branchIds];

  let page = 1;
  while (true) {
    const response = await branchApi.list({ is_active: true, page, page_size: 500 });
    for (const branch of response.items ?? []) {
      branchIds.add(String(branch.id));
    }
    const totalPages = Number(response.total_pages ?? page);
    if (!response.has_next && page >= totalPages) break;
    page += 1;
  }
  return [...branchIds];
}

async function mapLimit<T, R>(
  items: T[],
  limit: number,
  worker: (item: T) => Promise<R>,
): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let nextIndex = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (nextIndex < items.length) {
      const index = nextIndex;
      nextIndex += 1;
      results[index] = await worker(items[index]);
    }
  });
  await Promise.all(runners);
  return results;
}

async function runHydration(context: ContractCacheContext): Promise<ContractCacheResult> {
  const db = await getDb();
  const cache = await getPriceContractCacheInfo(
    db,
    context.organizationId,
    context.userId,
  );
  if (!context.force && cache.count > 0 && cache.newest) {
    const age = Date.now() - Date.parse(cache.newest);
    if (Number.isFinite(age) && age >= 0 && age < CACHE_TTL_MS) {
      return { ok: true, skipped: true, branchCount: 0, contractCount: cache.count };
    }
  }

  const branchIds = await resolveBranchIds(context);
  const successful: BranchAvailability[] = [];

  await mapLimit(branchIds, FETCH_CONCURRENCY, async (branchId) => {
    try {
      const contracts = await contractsApi.getAvailableForPos(branchId);
      successful.push({ branchId, contracts: contracts ?? [] });
    } catch (error) {
      const status = errorStatus(error);
      const isActiveBranch = branchId === context.activeBranchId;
      if (!isActiveBranch && (status === 401 || status === 403 || status === 404)) {
        return;
      }
      throw error;
    } finally {
      // Keep the branch order stable regardless of which request finished first.
      successful.sort((a, b) => a.branchId.localeCompare(b.branchId));
    }
  });

  await replacePriceContractAvailability(db, {
    scope: "user",
    organizationId: context.organizationId,
    userId: context.userId,
    branches: successful,
  });

  return {
    ok: true,
    skipped: false,
    branchCount: successful.length,
    contractCount: successful.reduce((sum, branch) => sum + branch.contracts.length, 0),
  };
}

export function hydratePriceContractAvailability(
  context: ContractCacheContext,
): Promise<ContractCacheResult> {
  const key = `${context.organizationId}:${context.userId}`;
  const existing = inflight.get(key);
  if (existing) return existing;

  const promise = runHydration(context)
    .catch((error) => {
      console.warn("[contractCache] hydration failed:", error);
      return {
        ok: false,
        skipped: false,
        branchCount: 0,
        contractCount: 0,
        error: errorMessage(error),
      };
    })
    .finally(() => {
      inflight.delete(key);
    });

  inflight.set(key, promise);
  return promise;
}

export type { AvailableContract };
