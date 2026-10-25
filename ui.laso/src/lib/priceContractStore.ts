import type { Database } from "@/lib/localDb";
import type { AvailableContract } from "@/api/contracts";

export interface BranchAvailability {
  branchId: string;
  contracts: AvailableContract[];
}

export interface ReplaceAvailabilityParams {
  scope: "user" | "branch";
  organizationId: string;
  userId: string;
  branchId?: string;
  branches: BranchAvailability[];
  cachedAt?: string;
}

const UPSERT_CONTRACT_SQL = `INSERT INTO price_contracts (
  id, organization_id, contract_code, contract_name, contract_type,
  is_default_contract, discount_type, discount_percentage,
  applies_to_prescription_only, applies_to_otc, applies_to_all_branches,
  applicable_branch_ids, effective_from, effective_to,
  requires_verification, requires_approval, daily_usage_limit,
  per_customer_usage_limit, insurance_provider_id, requires_preauthorization,
  minimum_purchase_amount, maximum_purchase_amount, status, is_active,
  copay_amount, copay_percentage, is_deleted,
  sync_status, sync_version, synced_at, updated_at, created_at
) VALUES (
  $1, $2, $3, $4, $5,
  $6, $7, $8,
  $9, $10, $11,
  $12, $13, $14,
  $15, $16, $17,
  $18, $19, $20,
  $21, $22, $23, $24,
  $25, $26, 0,
  'synced', 1, $27, $27, $27
)
ON CONFLICT(id) DO UPDATE SET
  organization_id = excluded.organization_id,
  contract_code = excluded.contract_code,
  contract_name = excluded.contract_name,
  contract_type = excluded.contract_type,
  is_default_contract = excluded.is_default_contract,
  discount_type = excluded.discount_type,
  discount_percentage = excluded.discount_percentage,
  applies_to_prescription_only = excluded.applies_to_prescription_only,
  applies_to_otc = excluded.applies_to_otc,
  applies_to_all_branches = excluded.applies_to_all_branches,
  applicable_branch_ids = excluded.applicable_branch_ids,
  effective_from = excluded.effective_from,
  effective_to = excluded.effective_to,
  requires_verification = excluded.requires_verification,
  requires_approval = excluded.requires_approval,
  daily_usage_limit = excluded.daily_usage_limit,
  per_customer_usage_limit = excluded.per_customer_usage_limit,
  insurance_provider_id = excluded.insurance_provider_id,
  requires_preauthorization = excluded.requires_preauthorization,
  minimum_purchase_amount = excluded.minimum_purchase_amount,
  maximum_purchase_amount = excluded.maximum_purchase_amount,
  status = excluded.status,
  is_active = excluded.is_active,
  copay_amount = excluded.copay_amount,
  copay_percentage = excluded.copay_percentage,
  is_deleted = 0,
  sync_status = 'synced',
  sync_version = price_contracts.sync_version,
  synced_at = excluded.synced_at,
  updated_at = excluded.updated_at,
  created_at = price_contracts.created_at`;

function contractValues(contract: AvailableContract, organizationId: string): unknown[] {
  const effectiveFrom = contract.effective_from ?? new Date().toISOString().slice(0, 10);
  return [
    contract.id,
    contract.organization_id ?? organizationId,
    contract.code,
    contract.name,
    contract.type,
    contract.is_default ? 1 : 0,
    contract.discount_type ?? "percentage",
    Number(contract.discount_percentage ?? 0),
    contract.applies_to_prescription_only ? 1 : 0,
    contract.applies_to_otc !== false ? 1 : 0,
    contract.applies_to_all_branches !== false ? 1 : 0,
    JSON.stringify(contract.applicable_branch_ids ?? []),
    effectiveFrom,
    contract.effective_to ?? null,
    contract.requires_verification ? 1 : 0,
    contract.requires_approval ? 1 : 0,
    contract.daily_usage_limit ?? null,
    contract.per_customer_usage_limit ?? null,
    contract.insurance_provider_id ?? null,
    contract.requires_preauthorization ? 1 : 0,
    contract.minimum_purchase_amount ?? null,
    contract.maximum_purchase_amount ?? null,
    contract.status ?? "active",
    contract.is_active !== false ? 1 : 0,
    contract.copay_amount ?? null,
    contract.copay_percentage ?? null,
  ];
}

export async function replacePriceContractAvailability(
  db: Database,
  params: ReplaceAvailabilityParams,
): Promise<void> {
  const cachedAt = params.cachedAt ?? new Date().toISOString();
  const statements: Array<{ sql: string; values: unknown[] }> = [];
  const contracts = new Map<string, AvailableContract>();

  if (params.scope === "branch") {
    if (!params.branchId) {
      throw new Error("branchId is required for branch-scoped availability");
    }
    statements.push({
      sql: `DELETE FROM price_contract_availability
             WHERE organization_id = $1 AND user_id = $2 AND branch_id = $3`,
      values: [params.organizationId, params.userId, params.branchId],
    });
  } else {
    statements.push({
      sql: `DELETE FROM price_contract_availability
             WHERE organization_id = $1 AND user_id = $2`,
      values: [params.organizationId, params.userId],
    });
  }

  for (const branch of params.branches) {
    for (const contract of branch.contracts) {
      if (!contracts.has(contract.id)) contracts.set(contract.id, contract);
      statements.push({
        sql: `INSERT INTO price_contract_availability
                (organization_id, user_id, branch_id, contract_id, cached_at)
              VALUES ($1, $2, $3, $4, $5)
              ON CONFLICT(user_id, branch_id, contract_id) DO UPDATE SET
                organization_id = excluded.organization_id,
                cached_at = excluded.cached_at`,
        values: [
          params.organizationId,
          params.userId,
          branch.branchId,
          contract.id,
          cachedAt,
        ],
      });
    }
  }

  for (const contract of contracts.values()) {
    statements.push({
      sql: UPSERT_CONTRACT_SQL,
      values: [...contractValues(contract, params.organizationId), cachedAt],
    });
  }

  if (db.executeTransaction) {
    await db.executeTransaction(statements);
    return;
  }

  await db.execute("BEGIN IMMEDIATE");
  try {
    for (const statement of statements) {
      await db.execute(statement.sql, statement.values);
    }
    await db.execute("COMMIT");
  } catch (error) {
    await db.execute("ROLLBACK");
    throw error;
  }
}

export async function getPriceContractCacheInfo(
  db: Database,
  organizationId: string,
  userId: string,
): Promise<{ count: number; newest: string | null }> {
  const rows = await db.select<{ count: number; newest: string | null }[]>(
    `SELECT COUNT(*) AS count, MAX(cached_at) AS newest
       FROM price_contract_availability
      WHERE organization_id = $1 AND user_id = $2`,
    [organizationId, userId],
  );
  return {
    count: Number(rows?.[0]?.count ?? 0),
    newest: rows?.[0]?.newest ?? null,
  };
}
